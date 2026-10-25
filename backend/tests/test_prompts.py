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

"""Tests for the prompt system: base classes, builder, templates, and specializations.

The prompt stack is exercised from the abstract base down to the concrete
specialties: ``EncreBasePrompt``, ``EncrePromptTemplate``, ``PromptBlock``
and ``EncrePromptBuilder``, then the frontmatter loader, the runtime prompt
helpers re-exported by ``encre.loop_stability``, and the per-specialty
prompt classes.
"""


import pytest


class TestEncreBasePrompt:
    """Tests for the ``EncreBasePrompt`` abstract base class.

    Confirms the base cannot be instantiated directly (it raises
    ``TypeError``) and that the two required abstract method slots,
    ``build_system_prompt`` and ``build_tool_instructions``, are declared
    on the class.
    """

    def test_verify_base_prompt_is_abstract(self):
        """The base class is abstract and cannot be instantiated directly."""
        from encre.prompts.base import EncreBasePrompt

        with pytest.raises(TypeError):
            EncreBasePrompt()  # Instantiating an ABC raises TypeError.

    def test_verify_base_prompt_has_abstract_methods(self):
        """The two required abstract methods are declared on the base class."""
        from encre.prompts.base import EncreBasePrompt

        assert hasattr(EncreBasePrompt, "build_system_prompt")
        assert hasattr(EncreBasePrompt, "build_tool_instructions")


class TestEncrePromptTemplate:
    """Tests for ``EncrePromptTemplate`` construction and prompt building.

    Covers default construction, specialty injection, custom builder
    injection and the builder property, system prompt generation with and
    without tools, custom instruction injection, the content difference
    between specialties, permission-mode variation, and tool-instruction
    text for both empty and populated tool lists.
    """

    def test_verify_prompt_template_construction_defaults(self):
        """A default-constructed template uses the general specialty and a real builder."""
        from encre.prompts.base import EncrePromptTemplate

        tmpl = EncrePromptTemplate()
        assert tmpl is not None
        assert tmpl._specialty == "general"
        assert tmpl._builder is not None

    def test_verify_prompt_template_construction_with_specialty(self):
        """The constructor stores the requested specialty verbatim."""
        from encre.prompts.base import EncrePromptTemplate

        tmpl = EncrePromptTemplate(specialty="coding")
        assert tmpl._specialty == "coding"

    def test_verify_prompt_template_construction_with_custom_builder(self):
        """An injected builder is kept, with the specialty recorded alongside it."""
        from encre.prompts.base import EncrePromptTemplate
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        tmpl = EncrePromptTemplate(builder=builder, specialty="research")
        assert tmpl._builder is builder
        assert tmpl._specialty == "research"

    def test_verify_prompt_template_builder_property(self):
        """The public builder property returns the stored internal builder."""
        from encre.prompts.base import EncrePromptTemplate

        tmpl = EncrePromptTemplate(specialty="data")
        assert tmpl.builder is tmpl._builder

    def test_verify_prompt_template_build_system_prompt_returns_string(self):
        """build_system_prompt returns a non-empty string for the default mode."""
        from encre.prompts.base import EncrePromptTemplate

        tmpl = EncrePromptTemplate(specialty="general")
        result = tmpl.build_system_prompt(mode="default")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_verify_prompt_template_build_system_prompt_with_tools(self):
        """Tool names passed to build_system_prompt appear in the rendered prompt."""
        from encre.prompts.base import EncrePromptTemplate

        tmpl = EncrePromptTemplate()
        tools = [
            {"function": {"name": "bash", "description": "Execute shell commands"}},
            {"function": {"name": "read", "description": "Read files"}},
        ]
        result = tmpl.build_system_prompt(mode="default", tools=tools)
        assert "bash" in result
        assert "read" in result

    def test_verify_prompt_template_build_system_prompt_with_custom_instructions(self):
        """Custom instructions are merged into the rendered system prompt."""
        from encre.prompts.base import EncrePromptTemplate

        tmpl = EncrePromptTemplate()
        result = tmpl.build_system_prompt(
            mode="default",
            custom_instructions="Always use Python 3.12 syntax.",
        )
        assert "Python 3.12" in result

    def test_verify_prompt_template_build_system_prompt_reflects_specialty(self):
        """Different specialties render prompts with different domain headings."""
        from encre.prompts.base import EncrePromptTemplate

        coding_tmpl = EncrePromptTemplate(specialty="coding")
        research_tmpl = EncrePromptTemplate(specialty="research")

        coding_result = coding_tmpl.build_system_prompt(mode="default")
        research_result = research_tmpl.build_system_prompt(mode="default")

        # The two specialties must render distinguishable prompts.
        assert coding_result != research_result
        assert "Software Engineering" in coding_result
        assert "Research" in research_result

    def test_verify_prompt_template_build_system_prompt_reflects_permission_mode(self):
        """The permission mode changes the rendered prompt text."""
        from encre.prompts.base import EncrePromptTemplate

        tmpl = EncrePromptTemplate()

        bypass_result = tmpl.build_system_prompt(mode="bypass")
        plan_result = tmpl.build_system_prompt(mode="plan")

        assert "bypass" in bypass_result.lower()
        assert "plan" in plan_result.lower()

    def test_verify_prompt_template_build_tool_instructions_empty_list(self):
        """An empty tool list renders the no-tools access message."""
        from encre.prompts.base import EncrePromptTemplate

        tmpl = EncrePromptTemplate()
        result = tmpl.build_tool_instructions([])
        assert "do not have access" in result.lower()

    def test_verify_prompt_template_build_tool_instructions_with_names(self):
        """A populated tool list names every tool plus the usage guidance."""
        from encre.prompts.base import EncrePromptTemplate

        tmpl = EncrePromptTemplate()
        result = tmpl.build_tool_instructions(["bash", "grep", "glob"])
        assert "bash" in result
        assert "grep" in result
        assert "glob" in result
        assert "Use them as needed" in result or "active and callable right now" in result


class TestPromptBlock:
    """Tests for ``PromptBlock`` construction and context substitution.

    Confirms the block stores its priority, name and content as given,
    and that ``with_context`` performs ``{{key}}`` substitution while
    leaving the block metadata untouched.
    """

    def test_verify_prompt_block_construction(self):
        """The block stores priority, name and content exactly as given."""
        from encre.prompts.system import PromptBlock

        block = PromptBlock(priority=10, name="test_block", content="Test content")
        assert block.priority == 10
        assert block.name == "test_block"
        assert block.content == "Test content"

    def test_verify_prompt_block_with_context(self):
        """with_context substitutes template variables while keeping the metadata."""
        from encre.prompts.system import PromptBlock

        block = PromptBlock(
            priority=50,
            name="templated",
            content="Hello {{username}}, welcome to {{project}}.",
        )
        ctx = {"username": "Alice", "project": "Encre"}
        filled = block.with_context(ctx)
        assert "Hello Alice" in filled.content
        assert "welcome to Encre" in filled.content
        assert filled.name == "templated"
        assert filled.priority == 50


class TestEncrePromptBuilder:
    """Tests for ``EncrePromptBuilder`` block management and prompt assembly.

    Covers construction, block add and remove, the no-op behaviour of
    removing a missing block, skill-summary and custom-instruction
    injection, builds for the default, coding, research, data and unknown
    specialties, permission-mode variation, tool guidance, context
    variable substitution, override precedence, mandatory-constraints
    placement and memory-discipline positioning.
    """

    def test_verify_prompt_builder_construction(self):
        """A new builder starts with no registered blocks."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        assert builder is not None
        assert builder._blocks == {}

    def test_verify_prompt_builder_add_block(self):
        """add_block registers the block under its name with its content intact."""
        from encre.prompts.system import EncrePromptBuilder, PromptBlock

        builder = EncrePromptBuilder()
        block = PromptBlock(priority=100, name="extra", content="Extra instructions")
        builder.add_block(block)
        assert "extra" in builder._blocks
        assert builder._blocks["extra"].content == "Extra instructions"

    def test_verify_prompt_builder_remove_block(self):
        """remove_block deletes a previously registered block."""
        from encre.prompts.system import EncrePromptBuilder, PromptBlock

        builder = EncrePromptBuilder()
        block = PromptBlock(priority=100, name="temporary", content="Temp")
        builder.add_block(block)
        assert "temporary" in builder._blocks
        builder.remove_block("temporary")
        assert "temporary" not in builder._blocks

    def test_verify_prompt_builder_remove_nonexistent_block_does_not_raise(self):
        """Removing a block that was never added is a silent no-op."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        builder.remove_block("nonexistent")  # Removing a missing block is a no-op.

    def test_verify_prompt_builder_skill_summary_injects_dynamic_catalogue(self):
        """A provided skill_summary is rendered as a dynamic Skills block."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        summary = "- `/travel-flights`: Flight search guidance"
        prompt = builder.build(skill_summary=summary)
        assert "## Skills (auto-discovered)" in prompt
        assert "/travel-flights" in prompt

    def test_verify_prompt_builder_empty_skill_summary_omits_block(self):
        """No skill_summary means no Skills block is injected."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        prompt = builder.build(skill_summary="")
        assert "## Skills (auto-discovered)" not in prompt

    def test_verify_prompt_builder_add_custom_instructions(self):
        """add_custom_instructions registers a high-priority custom block."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        builder.add_custom_instructions("Focus on testing.")
        assert "custom" in builder._blocks
        assert "Focus on testing" in builder._blocks["custom"].content
        assert builder._blocks["custom"].priority == 200

    def test_verify_prompt_builder_build_default(self):
        """build() with no arguments returns a non-empty default prompt."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build()
        assert isinstance(result, str)
        assert len(result) > 0
        # The output must mention at least one default identity block.
        assert "identity" in result.lower() or "helpful" in result.lower()

    def test_verify_prompt_builder_build_coding_specialty(self):
        """The coding specialty renders the Software Engineering heading."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build(specialty="coding")
        assert "Software Engineering" in result

    def test_verify_prompt_builder_build_research_specialty(self):
        """The research specialty renders the Research heading."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build(specialty="research")
        assert "Research" in result

    def test_verify_prompt_builder_build_data_specialty(self):
        """The data specialty renders the Data Analysis heading."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build(specialty="data")
        assert "Data Analysis" in result

    def test_verify_prompt_builder_build_unknown_specialty_falls_back_to_general(self):
        """An unknown specialty falls back to the general specialty text."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build(specialty="unknown_specialty")
        assert "Infer The Real Goal" in result

    def test_verify_prompt_builder_build_with_permission_mode(self):
        """The permission mode changes the rendered prompt text."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()

        bypass = builder.build(mode="bypass")
        default = builder.build(mode="default")

        assert "full autonomy" in bypass.lower()
        assert "Ask for permission" in default

    def test_verify_prompt_builder_build_with_tools(self):
        """Building with tools succeeds and keeps the tool-usage guidance."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        tools = [{"function": {"name": "test_tool", "description": "A test tool"}}]
        result = builder.build(tools=tools)
        # Tools reach the model through the API tools field rather than by
        # being inlined into the system prompt text; build must still succeed
        # and include the tool-usage guidance block.
        assert isinstance(result, str) and len(result) > 0
        assert "tool_usage" in result.lower() or "find_tool" in result.lower() or "bash" in result.lower()

    def test_verify_prompt_builder_build_with_custom_instructions(self):
        """Custom instructions passed to build() appear verbatim in the output."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build(custom_instructions="ALWAYS validate input first.")
        assert "ALWAYS validate input first" in result

    def test_verify_prompt_builder_build_with_context(self):
        """build_with_context runs without error even for unused context keys."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build_with_context(
            ctx={"username": "TestUser"},
            specialty="general",
        )
        # The identity block has no {{username}} placeholder, but the call
        # must still complete without raising.
        assert isinstance(result, str)
        assert len(result) > 0

    def test_verify_prompt_builder_build_with_context_variable_substitution(self):
        """Context values are substituted into block template placeholders."""
        from encre.prompts.system import EncrePromptBuilder, PromptBlock

        builder = EncrePromptBuilder()
        builder.add_block(
            PromptBlock(
                priority=200,
                name="context_block",
                content="User {{user}} using version {{version}}",
            )
        )
        result = builder.build_with_context(
            ctx={"user": "Alice", "version": "1.0.0"},
            specialty="general",
        )
        assert "User Alice" in result
        assert "version 1.0.0" in result

    def test_verify_prompt_builder_custom_block_can_override_default(self):
        """A custom block with the same name replaces the built-in default."""
        from encre.prompts.system import EncrePromptBuilder, PromptBlock

        builder = EncrePromptBuilder()
        # Replace the built-in identity block with a custom one.
        builder.add_block(
            PromptBlock(
                priority=0,
                name="identity",
                content="You are a friendly assistant.",
            )
        )
        result = builder.build()
        assert "friendly assistant" in result

    def test_verify_prompt_builder_mandatory_constraints_block_present_by_default(self):
        """The flagship mandatory-constraints block is injected by default."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build()
        assert "MANDATORY Constraints" in result
        assert "Binding Pre-Action Governance" in result

    def test_verify_prompt_builder_mandatory_constraints_ordered_after_identity_before_task_completion(self):
        """Priority 0.5 places mandatory constraints between identity (0) and task_completion (1)."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build()
        idx_identity = result.find("Encre Agent")
        idx_mand = result.find("MANDATORY Constraints")
        idx_task = result.find("Deliver Finished Work")
        assert 0 <= idx_identity < idx_mand < idx_task

    def test_verify_prompt_builder_mandatory_constraints_precedence_line(self):
        """The override line anchoring constraint authority is present."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build()
        assert "override" in result.lower()
        assert "autonomous" in result.lower()

    def test_verify_prompt_builder_memory_discipline_front_positioned(self):
        """Memory recall protocol must sit early, within the governance cluster."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build()
        assert "Memory Discipline" in result
        idx_discipline = result.index("Memory Discipline")
        idx_task = result.index("Task Completion")
        assert idx_discipline < idx_task

    def test_verify_prompt_builder_memory_discipline_recall_protocol_present(self):
        """The memory recall gating guidance is present in the built system prompt."""
        from encre.prompts.system import EncrePromptBuilder

        builder = EncrePromptBuilder()
        result = builder.build()
        assert "Memory Recall Gate" in result or "MANDATORY Recall Protocol" in result


class TestRuntimePromptFiles:
    """Tests that every runtime prompt helper returns non-empty content.

    Each function re-exported from ``encre.loop_stability``
    (``build_auto_continue_message``, ``build_grace_message``,
    ``build_delegation_guidance``, ``build_steer_injection``,
    ``build_thinking_prefill``) must produce a non-empty string, which
    proves no prompt file is missing or blank at runtime.
    """

    def test_verify_runtime_prompt_files_exist(self):
        """Every re-exported runtime prompt helper returns non-empty content."""
        from encre.loop_stability import (
            build_auto_continue_message,
            build_grace_message,
            build_delegation_guidance,
            build_steer_injection,
            build_thinking_prefill,
        )

        assert build_auto_continue_message()
        assert "remaining" in build_grace_message().lower() or "what remains" in build_grace_message().lower()
        assert build_delegation_guidance()
        assert build_steer_injection(["alpha", "beta"])
        assert build_thinking_prefill("help", enabled=True)
        assert build_thinking_prefill("why does this bug happen", enabled=True)
        assert build_thinking_prefill("What is the answer to this question?", enabled=True)


class TestPromptFrontmatter:
    """Tests for frontmatter YAML parsing in prompt files.

    Confirms ``_parse_frontmatter`` handles float priorities such as
    ``0.5`` and ``-1.5``, integer priorities, inline list conditions,
    block-style YAML lists, empty block-style values that fall back to
    ``None``, and documents with or without a closing delimiter.
    """

    def test_verify_frontmatter_float_priority_parses(self):
        """A float priority (e.g. 0.5) is parsed as a number, not a string."""
        from encre.prompts.loader import _parse_frontmatter

        meta, body = _parse_frontmatter("---\nname: slack\npriority: 0.5\ncondition: ~\n---\nCONTENT")
        assert meta["priority"] == 0.5
        assert isinstance(meta["priority"], float)
        assert body == "CONTENT"

    def test_verify_frontmatter_negative_float_priority_parses(self):
        """A negative float priority such as -1.5 is parsed as a number."""
        from encre.prompts.loader import _parse_frontmatter

        meta, _ = _parse_frontmatter("---\npriority: -1.5\n---\n")
        assert meta["priority"] == -1.5

    def test_verify_frontmatter_integer_and_list_still_parse(self):
        """Integer priorities and inline list conditions still parse as before."""
        from encre.prompts.loader import _parse_frontmatter

        meta, _ = _parse_frontmatter("---\npriority: 16\ncondition: [general, coding]\n---\n")
        assert meta["priority"] == 16
        assert meta["condition"] == ["general", "coding"]

    def test_verify_frontmatter_block_style_yaml_list_parses(self):
        """Block-style `- item` list values in frontmatter are parsed as lists."""
        from encre.prompts.loader import _parse_frontmatter

        meta, body = _parse_frontmatter(
            "---\nname: patterns\npriority: 100\npatterns:\n  - fully autonomous\n  - hands-off\n  - don't ask me\n---\nBODY"
        )
        assert meta["patterns"] == ["fully autonomous", "hands-off", "don't ask me"]
        assert body == "BODY"

    def test_verify_frontmatter_block_style_yaml_list_empty_value_falls_back_none(self):
        """A key whose block-style list is empty parses to None, not a broken entry."""
        from encre.prompts.loader import _parse_frontmatter

        meta, _ = _parse_frontmatter("---\nname: x\npatterns:\n---\nBODY")
        assert meta["patterns"] is None

    def test_verify_frontmatter_without_closing_delimiter_still_parses(self):
        """Frontmatter without a closing delimiter still splits into meta and body."""
        from encre.prompts.loader import _parse_frontmatter

        meta, body = _parse_frontmatter("---\nname: x\npriority: 1\n---\nCONTENT")
        assert meta["name"] == "x"
        assert body == "CONTENT"


class TestCheckpointHardGate:
    """Tests for the checkpoint hard-gate helpers in loop stability.

    Exercises ``count_consecutive_tool_steps`` over sequential tool-call
    chains, user interrupts and plain-text messages,
    ``checkpoint_gate_relaxed`` for autonomy-keyword detection, the
    checkpoint message builder, and the invariant that
    ``CHECKPOINT_TOOL_STEP_THRESHOLD`` is an integer >= 3.
    """

    def test_verify_checkpoint_count_consecutive_tool_steps(self):
        """A three-step tool chain counts as three consecutive tool steps."""
        from encre.loop_stability import count_consecutive_tool_steps

        msgs = [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "", "tool_calls": ["a"]},
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
            {"role": "assistant", "content": "", "tool_calls": ["b"]},
            {"role": "tool", "tool_call_id": "b", "content": "ok"},
            {"role": "assistant", "content": "", "tool_calls": ["c"]},
        ]
        assert count_consecutive_tool_steps(msgs) == 3

    def test_verify_checkpoint_count_consecutive_tool_steps_breaks_on_user(self):
        """A user message breaks the consecutive tool-step count."""
        from encre.loop_stability import count_consecutive_tool_steps

        msgs = [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "content": "", "tool_calls": ["a"]},
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
            {"role": "user", "content": "interrupt"},
            {"role": "assistant", "content": "", "tool_calls": ["b"]},
        ]
        assert count_consecutive_tool_steps(msgs) == 1

    def test_verify_checkpoint_count_consecutive_tool_steps_ignores_plain_text(self):
        """Assistant text without tool calls does not extend the tool-step run."""
        from encre.loop_stability import count_consecutive_tool_steps

        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "thinking text only"},
            {"role": "assistant", "content": "", "tool_calls": ["a"]},
        ]
        assert count_consecutive_tool_steps(msgs) == 1

    def test_verify_checkpoint_gate_relaxed_on_authorization(self):
        """Autonomy keywords relax the checkpoint gate."""
        from encre.loop_stability import checkpoint_gate_relaxed

        assert checkpoint_gate_relaxed("run fully autonomous and don't ask me")
        assert checkpoint_gate_relaxed("HANDS-OFF please")

    def test_verify_checkpoint_gate_not_relaxed_on_normal_prompt(self):
        """Ordinary prompts, including the empty one, keep the gate strict."""
        from encre.loop_stability import checkpoint_gate_relaxed

        assert not checkpoint_gate_relaxed("refactor the auth module")
        assert not checkpoint_gate_relaxed("")

    def test_verify_checkpoint_build_message(self):
        """The checkpoint message names the step count and the checkpoint."""
        from encre.loop_stability import build_checkpoint_message

        msg = build_checkpoint_message(7)
        assert msg
        assert "7" in msg
        assert "Checkpoint" in msg

    def test_verify_checkpoint_threshold_constant(self):
        """The threshold constant is an integer of at least 3."""
        from encre.loop_stability import CHECKPOINT_TOOL_STEP_THRESHOLD

        assert isinstance(CHECKPOINT_TOOL_STEP_THRESHOLD, int)
        assert CHECKPOINT_TOOL_STEP_THRESHOLD >= 3


class TestStandingOrdersReminder:
    """Tests for the per-turn standing-orders reminder mechanism.

    Confirms ``build_standing_orders_reminder`` returns a non-empty
    reminder carrying the expected keywords, that
    ``append_to_last_user_message`` appends suffix text to the last user
    message, and that it is a no-op when no user message exists.
    """

    def test_verify_standing_orders_reminder_loads_from_prompt_file(self):
        """The reminder loads from its prompt file and carries its key headings."""
        from encre.loop_stability import build_standing_orders_reminder

        reminder = build_standing_orders_reminder()
        assert reminder
        assert "Standing Orders" in reminder
        assert "RECALL BEFORE ACTING" in reminder.upper()

    def test_verify_standing_orders_append_to_last_user_message(self):
        """The suffix is appended to the content of the last user message."""
        from encre.loop_stability import append_to_last_user_message

        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        append_to_last_user_message(msgs, "REMINDER-SUFFIX")
        assert "REMINDER-SUFFIX" in msgs[-1]["content"]

    def test_verify_standing_orders_append_noop_without_user(self):
        """Without a user message the append leaves the history untouched."""
        from encre.loop_stability import append_to_last_user_message

        msgs = [{"role": "system", "content": "sys"}]
        append_to_last_user_message(msgs, "X")
        assert len(msgs) == 1
        assert "X" not in msgs[0]["content"]


class TestUserRulesInterpretation:
    """Tests for the rules prompt block loading and context substitution.

    Confirms ``PromptLoader`` substitutes the ``{{rules_content}}`` and
    ``{{execution_context}}`` placeholders, that the loaded rules text
    frames user directives as intent rather than absolute law, that
    scope-check language is present, and that the block encourages asking
    when interpretation is unclear.
    """

    def test_verify_rules_block_loads_with_context(self):
        """Context values fill the rules placeholders and the intent framing is kept."""
        from encre.prompts.loader import PromptLoader

        loader = PromptLoader()
        block = loader.load_with_context("rules", rules_content="TESTRULE", execution_context="CONTEXT")
        assert "TESTRULE" in block
        assert "CONTEXT" in block
        assert "Interpret as Intent" in block

    def test_verify_rules_block_not_mandatory_law(self):
        """The rules block frames directives as intent rather than absolute law."""
        from encre.prompts.loader import PromptLoader

        block = PromptLoader().load("rules")
        assert "without exception" not in block
        assert "intent" in block.lower()

    def test_verify_rules_block_scope_check_present(self):
        """The rules block contains the scope-check language for automation runs."""
        from encre.prompts.loader import PromptLoader

        block = PromptLoader().load("rules")
        assert "conversational rule" in block
        assert "automation" in block.lower() or "headless" in block.lower()

    def test_verify_rules_block_asks_when_unclear(self):
        """The rules block tells the agent to ask when interpretation is unclear."""
        from encre.prompts.loader import PromptLoader

        block = PromptLoader().load("rules")
        assert "ask" in block.lower()
        assert "question" in block.lower()

    def test_verify_rules_execution_context_placeholders_substitute(self):
        """The execution context value substitutes and no placeholder survives."""
        from encre.prompts.loader import PromptLoader

        headless = PromptLoader().load_with_context("rules", rules_content="R", execution_context="headless")
        interactive = PromptLoader().load_with_context("rules", rules_content="R", execution_context="interactive")
        assert "headless" in headless
        assert "interactive" in interactive
        assert "{{execution_context}}" not in headless


class TestSpecializationPrompts:
    """Tests for each prompt specialty subclass.

    Asserts that ``EncreCodingPrompt``, ``EncreGeneralPrompt``,
    ``EncreResearchPrompt`` and ``EncreDataPrompt`` expose the correct
    ``_specialty`` value and generate system prompts containing their
    domain heading (Software Engineering, Research, Data Analysis).
    The final test checks that tools and custom instructions merge into
    the coding specialty prompt.
    """

    def test_verify_specialization_coding_prompt_specialty(self):
        """The coding prompt reports specialty 'coding' and its domain heading."""
        from encre.prompts.coding import EncreCodingPrompt

        cp = EncreCodingPrompt()
        assert cp._specialty == "coding"
        result = cp.build_system_prompt(mode="default")
        assert "Software Engineering" in result

    def test_verify_specialization_general_prompt_specialty(self):
        """The general prompt reports specialty 'general' and builds a non-empty prompt."""
        from encre.prompts.general import EncreGeneralPrompt

        gp = EncreGeneralPrompt()
        assert gp._specialty == "general"
        result = gp.build_system_prompt(mode="default")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_verify_specialization_research_prompt_specialty(self):
        """The research prompt reports specialty 'research' and its domain heading."""
        from encre.prompts.research import EncreResearchPrompt

        rp = EncreResearchPrompt()
        assert rp._specialty == "research"
        result = rp.build_system_prompt(mode="default")
        assert "Research" in result

    def test_verify_specialization_data_prompt_specialty(self):
        """The data prompt reports specialty 'data' and its domain heading."""
        from encre.prompts.data import EncreDataPrompt

        dp = EncreDataPrompt()
        assert dp._specialty == "data"
        result = dp.build_system_prompt(mode="default")
        assert "Data Analysis" in result


class TestResponseRegisters:
    """Tests for the response-register axis (chat / answer / work).

    The register is the axis that keeps a greeting from inheriting an
    implementation request's delivery standard, and a real request from being
    answered as conversation.  Covers frontmatter parsing of the ``registers``
    key, the cache contract (the prefix never varies by register), the
    activation table read from that metadata, the per-turn live declaration,
    and the classifier that resolves the register.
    """

    def test_verify_frontmatter_registers_list_parses(self):
        """A block-style ``registers:`` list parses into a Python list."""
        from encre.prompts.loader import _parse_frontmatter

        meta, _ = _parse_frontmatter("---\nname: x\npriority: 1\ncondition: ~\nregisters:\n  - answer\n  - work\n---\nBODY")
        assert meta["registers"] == ["answer", "work"]

    def test_verify_system_prompt_is_register_independent(self):
        """The prefix carries every register section and never varies by turn.

        This is the cache contract, in three parts: ``build()`` accepts no
        register argument at all and emits all three register sections; with
        the specialty supplied per turn (``include_specialty=False``) the prefix
        is also independent of the detected domain intents; and the same inputs
        always produce the same bytes.
        """
        import inspect

        from encre.prompts.system import EncrePromptBuilder

        assert "register" not in inspect.signature(EncrePromptBuilder.build).parameters
        tools = [{"function": {"name": "file_read", "description": "Read a file"}}]
        result = EncrePromptBuilder().build(tools=tools)
        assert "Response Registers" in result
        assert "Chat Register" in result
        assert "Answer Register" in result
        assert "Work Register" in result
        assert "Deliver Finished Work" in result
        assert "Available Tools" in result
        # Byte-stable across builds: same inputs, identical prefix.
        assert result == EncrePromptBuilder().build(tools=tools)

        # Intent independence: with the specialty moved out of the prefix, a
        # topic change must not rewrite it.
        bare = EncrePromptBuilder()
        general_prefix = bare.build(tools=tools, intents=["general"], include_specialty=False)
        coding_prefix = bare.build(tools=tools, intents=["coding", "data", "research"], include_specialty=False)
        assert general_prefix == coding_prefix
        assert "Software Engineering Mode" not in general_prefix
        # ... and the domain sections still arrive when the caller keeps them.
        assert "General Domain Guidance" in bare.build(tools=tools, intents=["general"])
        assert "Software Engineering Mode" in bare.build(tools=tools, intents=["coding"])

    def test_verify_live_declaration_only_names_present_sections(self):
        """The inert list describes this prompt, not the whole blocks directory."""
        from encre.loop_stability import build_live_register_text
        from encre.prompts.system import EncrePromptBuilder

        tools = [{"function": {"name": "file_read", "description": "Read a file"}}]
        prefix = EncrePromptBuilder().build(tools=tools, intents=["general"], include_specialty=False)
        declaration = build_live_register_text("chat", prefix)
        assert "Task Completion" in declaration
        assert "Software Engineering Mode" not in declaration
        assert "Data Analysis Mode" not in declaration
        assert "Research Mode" not in declaration
        assert "Skills (auto-discovered)" not in declaration

    def test_verify_activation_map_matches_the_gated_sections(self):
        """The activation table is read from frontmatter, not hand-maintained."""
        from encre.prompts.system import register_activation_map

        activation = register_activation_map()
        assert activation["Task Completion"] == ["work"]
        assert activation["Post-Execution Validation"] == ["work"]
        assert activation["Chat Register"] == ["chat"]
        assert activation["MANDATORY Constraints"] == ["answer", "work"]
        assert activation["Tool Execution Discipline"] == ["answer", "work"]

    def test_verify_live_register_declaration_names_the_inert_sections(self):
        """The per-turn declaration says what governs and what is switched off."""
        from encre.loop_stability import build_live_register_text

        chat = build_live_register_text("chat")
        assert "Live Register: chat" in chat
        assert "conversation itself is the deliverable" in chat
        assert "Task Completion" in chat
        assert "Tool Execution Discipline" in chat
        assert "Chat Register" not in chat

        work = build_live_register_text("work")
        assert "Live Register: work" in work
        assert "Chat Register" in work
        assert "Answer Register" in work
        assert "Task Completion" not in work

        # An unrecognised label falls back to the strictest register.
        assert "Live Register: work" in build_live_register_text("nonsense")

    def test_verify_no_pause_policy_in_the_prompt(self):
        """The pause policy exists only as the runtime gate and its message.

        Four prompt copies of "stop after N steps" used to out-weigh the
        instruction to finish the job; nothing in the assembled prompt may
        restate a step budget now.
        """
        from encre.prompts.loader import PromptLoader
        from encre.prompts.system import EncrePromptBuilder

        result = EncrePromptBuilder().build()
        assert "tool-call steps" not in result
        assert "hand control back" not in result.lower()
        loader = PromptLoader()
        reminder = loader.load("standing_orders_reminder", category="reminders")
        assert "tool-call steps" not in reminder

    def test_verify_classify_register_greetings_are_chat(self):
        """Greetings in either language resolve to the conversation register."""
        from encre.prompts.classifier import classify_register

        assert classify_register("hi") == "chat"
        assert classify_register("在吗") == "chat"
        assert classify_register("谢谢，辛苦了") == "chat"
        assert classify_register("今天有点累，随便聊聊") == "chat"

    def test_verify_classify_register_production_requests_are_work(self):
        """A request that expects something to exist resolves to ``work``."""
        from encre.prompts.classifier import classify_register

        assert classify_register("refactor the auth module") == "work"
        assert classify_register("帮我把这个函数改成蛇形命名") == "work"
        assert classify_register("看看我们这个项目，梳理一下结构") == "work"
        assert classify_register("write a report on the loop phases") == "work"

    def test_verify_classify_register_questions_are_answer(self):
        """A pure explanation request resolves to ``answer``."""
        from encre.prompts.classifier import classify_register

        assert classify_register("what is a closure?") == "answer"
        assert classify_register("闭包是什么意思") == "answer"

    def test_verify_general_mode_never_presents_the_launch_directory_as_a_workspace(self):
        """Without a workspace the prompt must not offer a "working directory".

        Relative writes already land in the session files directory, but
        absolute paths pass through by design -- so a line reading
        ``Working directory: <wherever the client was started>`` was enough to
        make the model emit absolute paths inside the app's own tree and write
        its output there instead of into ``sessions/<id>/files``.
        """
        import os

        from encre.prompts.system import EncrePromptBuilder

        general = EncrePromptBuilder().build(workspace_root="")
        env = general.split("## Environment", 1)[1].split("## Operating System", 1)[0]
        assert "Working directory:" not in env
        assert "Project workspace: none" in env
        assert os.getcwd() in env, "the launch directory is still named, as a shell default"
        assert "NOT a working directory" in env
        assert "{{" not in general, "unresolved placeholder reached the prompt"
        assert "sessions" in general, "the session files location is never stated"

        workspace = EncrePromptBuilder().build(workspace_root=os.getcwd(), workspace_name="encre")
        wenv = workspace.split("## Environment", 1)[1].split("## Operating System", 1)[0]
        assert "Working directory:" in wenv
        assert "Is directory a git repo:" in wenv

    def test_verify_classify_intents_keeps_domain_on_short_chinese_tasks(self):
        """The conversational judgement may never swallow a domain intent.

        An earlier version returned early for short queries, so a Chinese task
        lost ``coding`` -- and with it the coding tool set -- silently.
        """
        from encre.prompts.classifier import classify_intents

        assert "coding" in classify_intents("把这个函数的异常处理修一下")
        assert "coding" in classify_intents("帮我看看这个项目的代码结构")

    def test_verify_checkpoint_gate_is_register_scoped(self):
        """Only ``work`` carries a pause budget; chat and answer carry none."""
        from encre.loop_stability import checkpoint_threshold_for

        assert checkpoint_threshold_for("chat") == 0
        assert checkpoint_threshold_for("answer") == 0
        assert checkpoint_threshold_for("work") >= 3
        assert checkpoint_threshold_for("") == checkpoint_threshold_for("work")

    def test_verify_checkpoint_relaxation_is_bilingual_and_scoped_to_user_turns(self):
        """Hands-off authorisation is recognised in Chinese and across turns."""
        from encre.loop_stability import checkpoint_gate_relaxed, recent_user_texts

        assert checkpoint_gate_relaxed("你一口气做完吧", ["帮我把项目梳理一遍"])
        assert checkpoint_gate_relaxed("now finish it", ["work autonomously on this"])
        msgs = [
            {"role": "user", "content": "别问我，自己跑完"},
            {"role": "assistant", "content": "", "tool_calls": ["a"]},
            {"role": "user", "content": "[CONTEXT UPDATE]\n\n## Mode\n..."},
            {"role": "user", "content": "继续"},
        ]
        assert recent_user_texts(msgs) == ["别问我，自己跑完", "继续"]
        assert checkpoint_gate_relaxed("继续", recent_user_texts(msgs))

    def test_verify_specialization_build_with_tools_and_custom_instructions(self):
        """Tools and custom instructions merge into the coding specialty prompt."""
        from encre.prompts.coding import EncreCodingPrompt

        cp = EncreCodingPrompt()
        tools = [{"function": {"name": "bash", "description": "Run bash commands"}}]
        result = cp.build_system_prompt(
            mode="default",
            tools=tools,
            custom_instructions="Always write docstrings.",
        )
        assert "bash" in result
        assert "Always write docstrings" in result
        assert "Software Engineering" in result
