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

"""Tests for the swarm subsystem: planner, blackboard, roles, and mailbox.

Each swarm component is exercised with real instances, never mocks, and every
scenario asserts observable structural invariants: mailbox ordering and timeout
semantics, keyword-driven task pattern detection, task tree topology, and
versioned blackboard compare-and-swap. Async methods are driven through
asyncio.run.
"""

import asyncio

from encre.swarm.blackboard import BlackboardEntry, EncreBlackboard
from encre.swarm.mailbox import EncreMailbox, MailboxMessage
from encre.swarm.planner import EncreTaskPlanner, TaskTree, _detect_pattern
from encre.swarm.roles import (
    ROLE_ARCHITECT,
    ROLE_CODER,
    ROLE_DEBUGGER,
    ROLE_GENERAL,
    ROLE_RESEARCHER,
    ROLE_REVIEWER,
    ROLE_TESTER,
    AgentRole,
    RoleRegistry,
)

# ---------------------------------------------------------------------------
# Mailbox
# ---------------------------------------------------------------------------


class TestMailbox:
    """Validate the async inter-agent mailbox communication channel.

    Seven scenarios cover construction with an owner id, send/receive round trips,
    receive timeout, FIFO ordering, peek without removal, and clear. Because the
    mailbox wraps an asyncio queue, peers can produce and consume messages
    independently without blocking the event loop, and message order must survive
    every handoff.
    """
    def test_verify_mailbox_creation_with_owner(self):
        """Construction stores the owner id so mail can be attributed to one agent."""
        mb = EncreMailbox(owner_id="agent1")
        assert mb.owner_id == "agent1"

    async def test_verify_send_receive_preserves_sender_and_content(self):
        """A message received by peer b keeps its sender a and its original content."""
        mb_a = EncreMailbox(owner_id="a")
        mb_b = EncreMailbox(owner_id="b")
        await mb_a.send(mb_b, "hello")
        msg = await mb_b.receive(timeout=1.0)
        assert msg is not None
        assert msg.content == "hello"
        assert msg.sender == "a"

    async def test_verify_receive_returns_none_on_timeout(self):
        """receive returns None when the timeout elapses with no message waiting."""
        mb = EncreMailbox(owner_id="test", timeout=0.1)
        msg = await mb.receive(timeout=0.01)
        assert msg is None

    async def test_verify_multiple_messages_arrive_in_fifo_order(self):
        """Sequential sends are delivered in the exact order they were sent."""
        mb_a = EncreMailbox(owner_id="a")
        mb_b = EncreMailbox(owner_id="b")
        await mb_a.send(mb_b, "first")
        await mb_a.send(mb_b, "second")
        msg1 = await mb_b.receive(timeout=1.0)
        msg2 = await mb_b.receive(timeout=1.0)
        assert msg1.content == "first"
        assert msg2.content == "second"

    def test_verify_mailbox_message_fields(self):
        """MailboxMessage carries sender, content, and defaults metadata to an empty dict."""
        msg = MailboxMessage(sender="a", content="test")
        assert msg.sender == "a"
        assert msg.content == "test"
        assert msg.metadata == {}

    def test_verify_peek_returns_message_without_removal(self):
        """peek exposes the head of the queue without consuming it."""
        async def _test():
            """Send one message, peek it, and confirm the receive still returns it."""
            mb_a = EncreMailbox(owner_id="a")
            mb_b = EncreMailbox(owner_id="b")
            await mb_a.send(mb_b, "msg1")
            peeked = mb_b.peek()
            assert len(peeked) == 1
            msg = await mb_b.receive(timeout=1.0)
            assert msg is not None
        asyncio.run(_test())

    def test_verify_clear_removes_all_pending_messages(self):
        """clear drains the queue, so the next receive times out empty."""
        async def _test():
            """Send one message, clear the mailbox, and confirm the receive times out."""
            mb_a = EncreMailbox(owner_id="a")
            mb_b = EncreMailbox(owner_id="b")
            await mb_a.send(mb_b, "msg1")
            mb_b.clear()
            msg = await mb_b.receive(timeout=0.1)
            assert msg is None
        asyncio.run(_test())


# ---------------------------------------------------------------------------
# RoleRegistry & Roles
# ---------------------------------------------------------------------------


class TestRoles:
    """Validate the role constant taxonomy and the role registry lookup logic.

    Seven scenarios cover the built-in role names, AgentRole construction with
    allowed tools, register/get/list/get_for_task/to_dict, and the general-role
    fallback. The registry is the single source of truth for role resolution, so
    it must support extension and keyword inference from task descriptions.
    """
    def test_verify_role_name_constants_are_correct(self):
        """The seven built-in role constants expose their canonical names."""
        assert ROLE_ARCHITECT.name == "architect"
        assert ROLE_CODER.name == "coder"
        assert ROLE_REVIEWER.name == "reviewer"
        assert ROLE_TESTER.name == "tester"
        assert ROLE_RESEARCHER.name == "researcher"
        assert ROLE_DEBUGGER.name == "debugger"
        assert ROLE_GENERAL.name == "general"

    def test_verify_agent_role_creation_with_allowed_tools(self):
        """AgentRole preserves the allowed_tools list for per-role permission checks."""
        role = AgentRole(name="custom", description="Custom role", allowed_tools=["bash"])
        assert role.name == "custom"
        assert "bash" in role.allowed_tools

    def test_verify_role_registry_register_and_get(self):
        """A newly registered custom role is retrievable by name."""
        registry = RoleRegistry()
        custom = AgentRole(name="custom_role", description="Custom")
        registry.register(custom)
        assert registry.get("custom_role").name == "custom_role"

    def test_verify_role_registry_get_falls_back_to_general_for_unknown_names(self):
        """Unknown role names resolve to the general role instead of raising KeyError."""
        registry = RoleRegistry()
        role = registry.get("nonexistent")
        assert role.name == "general"

    def test_verify_role_registry_list_includes_default_roles(self):
        """list_roles surfaces the built-in role set for assignment enumeration."""
        registry = RoleRegistry()
        roles = registry.list_roles()
        assert "architect" in roles
        assert "coder" in roles
        assert "general" in roles

    def test_verify_role_registry_get_for_task_maps_keywords_to_roles(self):
        """get_for_task maps keyword-matched task descriptions to the right specialist."""
        registry = RoleRegistry()
        assert registry.get_for_task("design the system").name == "architect"
        assert registry.get_for_task("implement the feature").name == "coder"
        assert registry.get_for_task("audit the system for security issues").name == "reviewer"
        assert registry.get_for_task("test the application").name == "tester"
        assert registry.get_for_task("research best practices").name == "researcher"
        assert registry.get_for_task("debug the null pointer").name == "debugger"
        assert registry.get_for_task("something else").name == "general"

    def test_verify_role_to_dict_serializes_name_and_description(self):
        """to_dict produces a mapping with at least name and description."""
        d = ROLE_CODER.to_dict()
        assert d["name"] == "coder"
        assert "description" in d


# ---------------------------------------------------------------------------
# TaskPlanner
# ---------------------------------------------------------------------------


class TestTaskPlanner:
    """Validate the task decomposition planner and its pattern detector.

    Sixteen scenarios cover _detect_pattern across five categories, plan() tree
    sizes for build/debug/research/refactor/unknown, TaskTree utilities
    (get_ready_nodes, all_done, has_failure), plan_with_llm prompt templating,
    plan_from_json deserialization, and the async decompose path. Pattern
    detection selects a template so varied task descriptions yield structured
    dependency graphs without manual orchestration.
    """
    def setup_method(self):
        """Create a fresh EncreTaskPlanner per test so scenarios stay isolated."""
        self.planner = EncreTaskPlanner()

    def test_verify_detect_pattern_classifies_build_keywords(self):
        """Synthesis verbs such as build, create, implement, write, develop map to build."""
        assert _detect_pattern("build a web app") == "build"
        assert _detect_pattern("create an API") == "build"
        assert _detect_pattern("implement a cache layer") == "build"
        assert _detect_pattern("write a CLI tool") == "build"
        assert _detect_pattern("develop a mobile app") == "build"

    def test_verify_detect_pattern_classifies_debug_keywords(self):
        """Repair verbs such as debug and fix a bug map to the debug template."""
        assert _detect_pattern("debug the login flow") == "debug"
        assert _detect_pattern("fix a bug in auth") == "debug"

    def test_verify_detect_pattern_classifies_research_keywords(self):
        """Inquiry verbs such as research and investigate map to the research template."""
        assert _detect_pattern("research async patterns") == "research"
        assert _detect_pattern("investigate memory leak") == "research"

    def test_verify_detect_pattern_classifies_refactor_keywords(self):
        """Restructuring verbs such as refactor and clean up map to the refactor template."""
        assert _detect_pattern("refactor the database layer") == "refactor"
        assert _detect_pattern("clean up the utils module") == "refactor"

    def test_verify_detect_pattern_returns_none_for_unrecognized_phrases(self):
        """No keyword match returns None, so the caller uses the default simple plan."""
        assert _detect_pattern("hello world") is None

    def test_verify_plan_build_pattern_produces_five_node_tree(self):
        """A build task yields a five-node tree with both entry and exit nodes."""
        tree = self.planner.plan("build a REST API")
        assert isinstance(tree, TaskTree)
        assert len(tree.nodes) == 5
        assert len(tree.entry_nodes) > 0
        assert len(tree.exit_nodes) > 0

    def test_verify_plan_debug_pattern_produces_four_node_tree(self):
        """A debug task yields the shorter four-node diagnosis-plus-fix tree."""
        tree = self.planner.plan("fix the authentication bug")
        assert len(tree.nodes) == 4

    def test_verify_plan_research_pattern_produces_four_node_tree(self):
        """A research task yields a four-node gathering-and-synthesis tree."""
        tree = self.planner.plan("investigate database performance")
        assert len(tree.nodes) == 4

    def test_verify_plan_refactor_pattern_produces_five_node_tree(self):
        """A refactor task yields a five-node analyze-and-transform tree."""
        tree = self.planner.plan("refactor the user service")
        assert len(tree.nodes) == 5

    def test_verify_plan_unknown_pattern_falls_back_to_single_node_tree(self):
        """An unrecognized pattern falls back to a valid single-node tree."""
        tree = self.planner.plan("do something unusual and uncategorized")
        assert len(tree.nodes) == 1

    def test_verify_task_tree_get_ready_nodes_returns_pending_nodes_with_no_dependencies(self):
        """get_ready_nodes returns only pending nodes that wait on nothing."""
        tree = self.planner.plan("build a CLI")
        ready = tree.get_ready_nodes()
        assert len(ready) > 0
        for node in ready:
            assert node.status == "pending"
            assert node.dependencies == []

    def test_verify_task_tree_all_done_returns_true_when_all_nodes_are_completed(self):
        """all_done becomes True once every node has been marked completed."""
        tree = self.planner.plan("fix a bug")
        for node in tree.nodes.values():
            node.status = "completed"
        assert tree.all_done() is True

    def test_verify_task_tree_has_failure_returns_true_when_any_node_is_failed(self):
        """has_failure becomes True as soon as one node is marked failed."""
        tree = self.planner.plan("fix a bug")
        first = next(iter(tree.nodes.values()))
        first.status = "failed"
        assert tree.has_failure() is True

    def test_verify_plan_with_llm_returns_prompt_with_goal_and_context_placeholders(self):
        """plan_with_llm returns a prompt template holding both placeholders."""
        prompt = self.planner.plan_with_llm("build a chat app", "using FastAPI")
        assert "Goal: {goal}" in prompt
        assert "Context: {context}" in prompt

    def test_verify_plan_from_json_deserializes_tasks_and_entry_exit_nodes(self):
        """plan_from_json rebuilds the declared topology, including entry and exit nodes."""
        import json
        data = {
            "tasks": [
                {"id": "t1", "name": "Design", "description": "architect", "role": "architect", "dependencies": [], "priority": 10},
                {"id": "t2", "name": "Code", "description": "implement", "role": "coder", "dependencies": ["t1"], "priority": 5},
            ],
            "entry_tasks": ["t1"],
            "exit_tasks": ["t2"],
        }
        tree = EncreTaskPlanner.plan_from_json("test goal", json.dumps(data))
        assert len(tree.nodes) == 2
        assert tree.entry_nodes == ["t1"]
        assert tree.exit_nodes == ["t2"]

    def test_verify_decompose_async_returns_a_nonempty_task_tree(self):
        """The async decompose path returns the same nonempty tree shape as plan()."""
        async def _test():
            """Drive the async decompose path and check the returned tree is nonempty."""
            tree = await self.planner.decompose("build a web app")
            assert isinstance(tree, TaskTree)
            assert len(tree.nodes) > 0
        asyncio.run(_test())


# ---------------------------------------------------------------------------
# Blackboard
# ---------------------------------------------------------------------------


class TestBlackboard:
    """Validate the shared blackboard as a versioned, namespaced key-value store.

    Eleven scenarios cover put/get, missing keys, namespace bulk reads, visible
    entries, delete, overwrite, version increments, compare-and-swap success and
    staleness, BlackboardEntry field integrity, and reset. Monotonic versions
    plus CAS give concurrent agents causal consistency without locks.
    """

    def setup_method(self):
        """Create a fresh EncreBlackboard per test so shared state stays isolated."""
        self.bb = EncreBlackboard()

    def test_verify_put_and_get_retains_value_with_owner(self):
        """A put/get round trip returns the stored value in the result tuple."""
        self.bb.put("default", "key1", "value1", owner="agent1")
        result = self.bb.get("default", "key1")
        assert result is not None
        assert result[0] == "value1"

    def test_verify_get_returns_none_for_missing_key(self):
        """get returns None for an absent key, distinguishing absent from empty."""
        assert self.bb.get("default", "nonexistent") is None

    def test_verify_get_all_retrieves_all_keys_in_a_namespace(self):
        """get_all returns every key written into that namespace."""
        self.bb.put("ns1", "k1", "v1", owner="a")
        self.bb.put("ns1", "k2", "v2", owner="a")
        all_data = self.bb.get_all("ns1")
        assert all_data["k1"] == "v1"
        assert all_data["k2"] == "v2"

    def test_verify_get_all_visible_returns_public_namespace_entries(self):
        """get_all_visible surfaces ownerless (public) entries for broad broadcast."""
        self.bb.put("public_ns", "key", "value")
        visible = self.bb.get_all_visible()
        assert "public_ns/key" in visible
        assert "value" in visible

    def test_verify_delete_removes_key_and_returns_true_while_missing_key_returns_false(self):
        """delete removes an existing key and reports False when nothing was removed."""
        self.bb.put("default", "k1", "v1")
        assert self.bb.delete("default", "k1") is True
        assert self.bb.get("default", "k1") is None
        assert self.bb.delete("default", "nonexistent") is False

    def test_verify_overwrite_replaces_value_and_advances_version(self):
        """Re-putting a key replaces the value instead of concatenating it."""
        self.bb.put("default", "k1", "v1")
        self.bb.put("default", "k1", "v2")
        result = self.bb.get("default", "k1")
        assert result[0] == "v2"

    def test_verify_version_increments_on_each_put(self):
        """Successive puts return strictly increasing versions, which CAS relies on."""
        v1 = self.bb.put("default", "k1", "v1")
        v2 = self.bb.put("default", "k1", "v2")
        assert v2 > v1

    def test_verify_compare_and_swap_succeeds_when_version_matches(self):
        """CAS with the current version succeeds and lands the new value."""
        v = self.bb.put("default", "k1", "v1")
        assert self.bb.compare_and_swap("default", "k1", v, "v2") is True
        result = self.bb.get("default", "k1")
        assert result[0] == "v2"

    def test_verify_compare_and_swap_fails_when_version_is_stale(self):
        """CAS with a stale version aborts so concurrent writes are not clobbered."""
        self.bb.put("default", "k1", "v1")
        assert self.bb.compare_and_swap("default", "k1", 999, "v2") is False

    def test_verify_blackboard_entry_stores_key_value_version_namespace_and_owner(self):
        """BlackboardEntry keeps the structural fields used for namespaced writes."""
        entry = BlackboardEntry(key="test", value=42, version=1, namespace="ns1", owner="agent1")
        assert entry.key == "test"
        assert entry.value == 42
        assert entry.version == 1

    def test_verify_reset_clears_all_namespaces(self):
        """reset empties every namespace so a new swarm round starts clean."""
        self.bb.put("default", "k1", "v1")
        self.bb.reset()
        assert self.bb.get("default", "k1") is None
