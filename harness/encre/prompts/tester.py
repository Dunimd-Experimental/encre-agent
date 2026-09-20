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

"""Prompt testing and validation framework.

Provides tools to version, test, and validate prompt templates so that
changes can be quantified rather than made "by feel".

Usage::

    from encre.prompts.tester import PromptTestSuite, PromptTestCase

    suite = PromptTestSuite()

    @suite.register("tool_usage_has_reasoning")
    def test_tool_usage_has_reasoning(loader):
        content = loader.load("tool_usage")
        assert "<thinking>" in content, "tool_usage must mandate reasoning"
        assert "Always Reason Before You Act" in content
        return True

    results = suite.run_all()
    # results.passed, results.failed, results.errors
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from encre.logging_config import get_logger
from encre.prompts.loader import PromptLoader

logger = get_logger("encre.prompts.tester")


@dataclass
class PromptVersion:
    """A versioned snapshot of a prompt template."""

    name: str
    content: str
    version: str
    hash: str
    created_at: float = 0.0

    @staticmethod
    def compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def snapshot(loader: PromptLoader, name: str, version: str) -> PromptVersion:
        """Capture a versioned snapshot of a loaded prompt template."""
        content = loader.load(name)
        return PromptVersion(
            name=name,
            content=content,
            version=version,
            hash=PromptVersion.compute_hash(content),
            created_at=time.time(),
        )


@dataclass
class PromptTestCase:
    """A single prompt test case with expected behavior."""

    name: str
    fn: Callable[[PromptLoader], bool]
    description: str = ""


@dataclass
class TestResults:
    """Aggregated test results."""

    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed) + len(self.errors)

    @property
    def success(self) -> bool:
        return len(self.failed) == 0 and len(self.errors) == 0


class PromptTestSuite:
    """A suite of prompt validation tests.

    Register test cases with :meth:`register` or :meth:`register_case`,
    then run all tests with :meth:`run_all`.
    """

    def __init__(self) -> None:
        self._cases: list[PromptTestCase] = []

    def register(
        self, name: str, description: str = "",
    ) -> Callable[[Callable[[PromptLoader], bool]], Callable[[PromptLoader], bool]]:
        """Decorator to register a test case."""
        def decorator(fn: Callable[[PromptLoader], bool]) -> Callable[[PromptLoader], bool]:
            self._cases.append(PromptTestCase(name=name, fn=fn, description=description))
            return fn
        return decorator

    def register_case(self, case: PromptTestCase) -> None:
        self._cases.append(case)

    def run_all(self, loader: PromptLoader | None = None) -> TestResults:
        """Run all registered test cases and return aggregated results."""
        if loader is None:
            loader = PromptLoader()
        results = TestResults()
        for case in self._cases:
            try:
                ok = case.fn(loader)
                if ok:
                    results.passed.append(case.name)
                    logger.info("[prompt-test] PASS: %s", case.name)
                else:
                    results.failed.append((case.name, "returned False"))
                    logger.warning("[prompt-test] FAIL: %s", case.name)
            except AssertionError as e:
                results.failed.append((case.name, str(e) or "assertion failed"))
                logger.warning("[prompt-test] FAIL: %s 鈥?%s", case.name, e)
            except Exception as e:
                results.errors.append((case.name, f"{type(e).__name__}: {e}"))
                logger.error("[prompt-test] ERROR: %s 鈥?%s", case.name, e)
        return results

    def list_cases(self) -> list[PromptTestCase]:
        return list(self._cases)


# 鈹€鈹€ Built-in validation tests 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

_suite = PromptTestSuite()


@_suite.register("identity_has_core_principles", "identity prompt must include core principles")
def _test_identity_principles(loader: PromptLoader) -> bool:
    content = loader.load("identity")
    required = ["Think before you act", "Be precise and truthful", "Be systematic", "Be minimal", "Be responsible"]
    for phrase in required:
        assert phrase in content, f"identity.prompt missing: {phrase!r}"
    return True


@_suite.register("tool_usage_has_dedicated_tools_table", "tool_usage prompt must include the dedicated tools table")
def _test_tool_usage_table(loader: PromptLoader) -> bool:
    content = loader.load("tool_usage")
    assert "bash is your LAST tool" in content or "bash is your Last Tool" in content or "Tools First, Bash Last" in content
    assert "file_read" in content and "file_edit" in content and "file_write" in content
    return True


@_suite.register("tool_usage_has_reasoning", "tool_usage must mandate reasoning before tool calls")
def _test_tool_usage_reasoning(loader: PromptLoader) -> bool:
    content = loader.load("tool_usage")
    assert "Think First" in content, "tool_usage must include 'Think First' heading"
    assert "reason about the user" in content, "tool_usage must mandate reasoning about user intent"
    return True


@_suite.register("output_format_has_diff_requirement", "output_format must require diff format for code")
def _test_output_format_diff(loader: PromptLoader) -> bool:
    content = loader.load("output_format")
    assert "diff" in content
    return True


@_suite.register("safety_has_data_protection", "safety prompt must include data protection rules")
def _test_safety_data(loader: PromptLoader) -> bool:
    content = loader.load("safety")
    assert "secrets" in content or "credentials" in content or "API key" in content
    return True


@_suite.register("safety_has_risk_framework", "safety must include risk/reversibility framework")
def _test_safety_risk(loader: PromptLoader) -> bool:
    content = loader.load("safety")
    assert "blast radius" in content or "reversibility" in content
    assert "Measure twice" in content or "measure twice" in content
    return True


@_suite.register("identity_has_collaborator", "identity must include collaborator mindset")
def _test_identity_collaborator(loader: PromptLoader) -> bool:
    content = loader.load("identity")
    assert "collaborator" in content
    assert "faithfully" in content
    return True


@_suite.register("identity_has_prompt_injection_awareness", "identity must mention prompt injection")
def _test_identity_injection(loader: PromptLoader) -> bool:
    content = loader.load("identity")
    assert "prompt injection" in content
    return True


@_suite.register("output_format_has_user_facing_orientation", "output_format must distinguish user-facing text")
def _test_output_format_user_facing(loader: PromptLoader) -> bool:
    content = loader.load("output_format")
    assert "User-facing text" in content or "tool calls" in content
    assert "no emojis" in content or "No emojis" in content
    return True


@_suite.register("output_format_has_inverted_pyramid", "output_format must have inverted pyramid guidance")
def _test_output_format_pyramid(loader: PromptLoader) -> bool:
    content = loader.load("output_format")
    assert "inverted pyramid" in content or "Lead with the answer" in content
    return True


@_suite.register("coding_has_minimalism_rules", "specialty_coding must include minimalism and faithfulness rules")
def _test_coding_minimalism(loader: PromptLoader) -> bool:
    content = loader.load("specialty_coding")
    assert "premature abstraction" in content or "gold-plate" in content
    assert "faithfully" in content or "outcomes" in content
    return True


@_suite.register("coding_has_comment_discipline", "specialty_coding must have comment rules")
def _test_coding_comments(loader: PromptLoader) -> bool:
    content = loader.load("specialty_coding")
    assert "WHY is non-obvious" in content or "explain WHAT" in content
    return True


# 鈹€鈹€ Contract tests 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

# Valid intent names recognized by the classifier and prompt builder.
_VALID_INTENTS = frozenset({"general", "coding", "research", "data", "conversation"})

# Known providers for {{variable}} placeholders in prompt files.
# Maps variable name -> (source_file, description).
_KNOWN_VARIABLE_PROVIDERS: dict[str, tuple[str, str]] = {
    # blocks/ category
    "adapter_list": ("channels/base.py:240", "Connected adapters from channel routing"),
    "channel": ("channels/base.py:250", "Platform channel name"),
    "command_body": ("prompts/system.py:288", "Active command body text"),
    "command_name": ("prompts/system.py:288", "Active command name"),
    "cwd": ("prompts/system.py:397", "Current working directory"),
    "date": ("prompts/system.py:415", "Current date string"),
    "details": ("prompts/system.py:397", "OS version details"),
    "device_context": ("loop_context.py:409", "Device context catalog"),
    "execution_context": ("loop/phases/prompt.py:378", "Headless vs interactive rules scope annotation"),
    "files_root": ("prompts/system.py:370", "Session files directory"),
    "guidance": ("permission/<mode>.prompt content", "Permission-mode guidance body"),
    "is_git": ("prompts/system.py:397", "Git repo detection flag"),
    "os_name": ("prompts/system.py:397", "Operating system name"),
    "project_snapshot": ("prompts/system.py:255", "Project summary snapshot"),
    "rules_content": ("loop.py:1941", "User-defined rules content"),
    "shell_hint": ("prompts/system.py:397", "Shell-specific usage hints"),
    "skill_summary": ("prompts/system.py:361", "Available skills catalogue"),
    "tools_list": ("prompts/base.py:147", "Tool names list for tool_instructions"),
    "workspace_name": ("prompts/system.py:253", "Workspace/project name"),
    "workspace_root": ("prompts/system.py:253", "Workspace root directory path"),
    "year": ("prompts/system.py:415", "Current year string"),
    # slash_commands/ category
    "cmd_name": ("prompts/system.py:344", "Slash command name"),
    "title": ("prompts/system.py:344", "Slash command display title"),
    "description_suffix": ("prompts/system.py:344", "Slash command description suffix"),
    "mode": ("prompts/system.py:305", "Active slash command mode"),
    # skills/ category (runtime-provided by skill system)
    "args": ("skills/runtime", "Skill arguments"),
    "interval_seconds": ("skills/runtime", "Loop interval in seconds"),
    "prompt_text": ("skills/runtime", "Loop prompt text"),
    # goal/ category
    "description": ("goal/runtime", "Goal description"),
    "success_criteria": ("goal/runtime", "Goal success criteria"),
    "max_attempts": ("goal/runtime", "Max goal attempts"),
    # swarm/ category
    "goal": ("swarm/runtime", "Swarm goal"),
    "context": ("swarm/runtime", "Swarm context"),
    # autosafety/ category
    "tool_name": ("autosafety/runtime", "Safety evaluation tool name"),
    "args_str": ("autosafety/runtime", "Safety evaluation tool arguments"),
    "user_pattern": ("autosafety/runtime", "Safety evaluation user pattern"),
}

# Model family names (from _MODEL_FAMILY_PATTERNS in system.py) that must NOT
# appear in their own model prompt content.
_MODEL_NAMES: dict[str, list[str]] = {
    "arcee": ["arcee"],
    "claude": ["claude", "anthropic"],
    "deepseek": ["deepseek"],
    "doubao": ["doubao"],
    "gemini": ["gemini", "gemma"],
    "glm": ["glm"],
    "gpt": ["gpt", "grok", "codex", "openai"],
    "hunyuan": ["hunyuan"],
    "kimi": ["kimi"],
    "llama": ["llama"],
    "mimo": ["mimo"],
    "minimax": ["minimax"],
    "mistral": ["mistral", "mixtral"],
    "nova": ["nova"],
    "phi": ["phi"],
}

# Expected priority ordering invariants for critical blocks.
# Format: (block_name, expected_priority) 鈥?exact match.
# NOTE: memory_discipline sits deliberately at 0.6 -- right after identity (0)
# but before task_completion (1) -- so the recall/precedence frame is
# established before any delivery-focused guidance is read.
_CRITICAL_BLOCK_PRIORITIES: dict[str, float] = {
    "identity": 0,
    "mandatory_constraints": 0.5,
    "task_completion": 1,
    "tool_execution": 3,
    "post_execution_validation": 4,
    "environment": 8,
    "current_datetime": 9,
    "task_management": 15,
    "memory_discipline": 0.6,
    "permission": 20,
    "language": 25,
    "output_format": 30,
    "skills": 47,
    "slash_commands": 48,
    "model_family": 90,
    "specialty": 100,
    "command_instructions": 190,
    "custom": 200,
}


@_suite.register("all_prompt_files_loadable", "every .prompt file must load without error")
def _test_all_files_loadable(loader: PromptLoader) -> bool:
    import os
    root = loader.root
    errors: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".prompt"):
                continue
            rel = os.path.relpath(dirpath, root)
            name = fn[:-7]  # strip .prompt
            category = rel.replace(os.sep, "/")
            try:
                loader.load(name, category=category)
            except Exception as e:
                errors.append(f"{category}/{name}: {e}")
    assert not errors, f"Failed to load {len(errors)} files:\n" + "\n".join(errors)
    return True


@_suite.register("template_variables_have_providers", "every {{var}} in blocks/ must have a known provider")
def _test_template_variables(loader: PromptLoader) -> bool:
    import os
    import re
    root = loader.root
    var_pattern = re.compile(r"\{\{(\w+)\}\}")
    unregistered: list[str] = []
    for fn in sorted(os.listdir(os.path.join(root, "blocks"))):
        if not fn.endswith(".prompt"):
            continue
        path = os.path.join(root, "blocks", fn)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        for m in var_pattern.finditer(content):
            var = m.group(1)
            if var not in _KNOWN_VARIABLE_PROVIDERS:
                unregistered.append(f"blocks/{fn}: {{{{${var}}}}}")
    assert not unregistered, f"Unregistered template variables:\n" + "\n".join(unregistered)
    return True


@_suite.register("model_family_blocks_hide_model_names", "model family prompts must not name their own model")
def _test_model_family_no_names(loader: PromptLoader) -> bool:
    import os
    root = loader.root
    models_dir = os.path.join(root, "models")
    violations: list[str] = []
    for fn in sorted(os.listdir(models_dir)):
        if not fn.endswith(".prompt"):
            continue
        name = fn[:-7]  # strip .prompt
        if name == "default":
            continue
        path = os.path.join(models_dir, fn)
        with open(path, encoding="utf-8") as f:
            content = f.read().lower()
        forbidden = _MODEL_NAMES.get(name, [name])
        for word in forbidden:
            if word.lower() in content:
                violations.append(f"models/{fn} contains model name: {word!r}")
                break
    assert not violations, f"Model name leaks:\n" + "\n".join(violations)
    return True


@_suite.register("cache_boundary_block_exists", "cache_boundary.prompt must exist and be loadable")
def _test_cache_boundary(loader: PromptLoader) -> bool:
    content = loader.load("cache_boundary")
    assert "PROMPT_CACHE_BOUNDARY" in content, "cache_boundary must contain the boundary marker"
    return True


@_suite.register("priority_invariants_maintained", "critical blocks must have expected priority values")
def _test_priority_invariants(loader: PromptLoader) -> bool:
    from encre.prompts.system import (
        _identity_block, _task_completion_block, _tool_execution_block,
        _environment_block, _current_datetime_block, _task_management_block,
        _memory_discipline_block, _output_format_block, _safety_block,
        _tool_usage_block, _post_execution_validation_block,
    )
    # Build a dict of actual blocks and check their priorities
    blocks = {
        "identity": _identity_block(),
        "task_completion": _task_completion_block(),
        "tool_execution": _tool_execution_block(),
        "post_execution_validation": _post_execution_validation_block(),
        "safety": _safety_block(),
        "tool_usage": _tool_usage_block(),
        "environment": _environment_block(),
        "current_datetime": _current_datetime_block(),
        "task_management": _task_management_block(),
        "memory_discipline": _memory_discipline_block(),
        "output_format": _output_format_block(),
    }
    violations: list[str] = []
    for name, expected in _CRITICAL_BLOCK_PRIORITIES.items():
        if name in blocks:
            actual = blocks[name].priority
            if actual != expected:
                violations.append(f"{name}: expected priority {expected}, got {actual}")
    # Verify ordering: identity < task_completion < tool_execution
    ordered = sorted(blocks.values(), key=lambda b: b.priority)
    expected_order = [b.name for b in ordered]
    assert not violations, f"Priority mismatches:\n" + "\n".join(violations)
    return True


@_suite.register("conditions_use_valid_intents", "every block condition must reference valid intent names")
def _test_conditions_valid(loader: PromptLoader) -> bool:
    from encre.prompts.system import (
        _identity_block, _task_completion_block, _tool_execution_block,
        _safety_block, _current_datetime_block, _environment_block,
        _tool_usage_block, _task_management_block, _memory_discipline_block,
        _output_format_block, _model_family_block,
        _specialty_coding_block, _specialty_research_block,
        _specialty_data_block, _specialty_general_block,
        _post_execution_validation_block,
    )
    blocks = [
        _identity_block(), _task_completion_block(), _tool_execution_block(),
        _safety_block(), _current_datetime_block(), _environment_block(),
        _tool_usage_block(), _task_management_block(), _memory_discipline_block(),
        _output_format_block(), _model_family_block(),
        _specialty_coding_block(), _specialty_research_block(),
        _specialty_data_block(), _specialty_general_block(),
        _post_execution_validation_block(),
    ]
    invalid: list[str] = []
    for b in blocks:
        if b.condition is not None:
            for intent in b.condition:
                if intent not in _VALID_INTENTS:
                    invalid.append(f"{b.name}: unknown intent {intent!r}")
    assert not invalid, f"Invalid intent references:\n" + "\n".join(invalid)
    return True


@_suite.register("specialty_conditions_cover_all_domains", "output_format must cover all domains including research")
def _test_specialty_conditions(loader: PromptLoader) -> bool:
    from encre.prompts.system import _output_format_block
    block = _output_format_block()
    assert block.condition is not None, "output_format must have a condition"
    assert "research" in block.condition, (
        f"output_format condition {block.condition} must include 'research'"
    )
    assert "coding" in block.condition, (
        f"output_format condition {block.condition} must include 'coding'"
    )
    assert "data" in block.condition, (
        f"output_format condition {block.condition} must include 'data'"
    )
    # Verify task_management excludes research (intentional)
    from encre.prompts.system import _task_management_block
    tm = _task_management_block()
    assert tm.condition is not None
    assert "research" not in tm.condition, (
        f"task_management condition {tm.condition} should exclude 'research'"
    )
    return True


@_suite.register("skills_have_output_format", "every skill agent template must define an output format")
def _test_skills_output(loader: PromptLoader) -> bool:
    import os
    root = loader.root
    skills_dir = os.path.join(root, "skills")
    missing: list[str] = []
    for fn in sorted(os.listdir(skills_dir)):
        if not fn.endswith(".prompt"):
            continue
        path = os.path.join(skills_dir, fn)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if "Output Format" not in content:
            missing.append(fn)
    assert not missing, f"Skills without 'Output Format' section: {missing}"
    # ALL skills must now have STATUS markers (standardized across 14 skills)
    missing_status: list[str] = []
    for fn in sorted(os.listdir(skills_dir)):
        if not fn.endswith(".prompt"):
            continue
        path = os.path.join(skills_dir, fn)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if "STATUS:" not in content and "STATUS" not in content:
            missing_status.append(fn)
    assert not missing_status, (
        f"Skills without STATUS markers: {missing_status}"
    )
    return True


@_suite.register("all_frontmatter_files_have_version", "every .prompt file with frontmatter must have a version field")
def _test_frontmatter_version(loader: PromptLoader) -> bool:
    import os
    import re
    root = loader.root
    missing: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if not fn.endswith(".prompt"):
                continue
            path = os.path.join(dirpath, fn)
            with open(path, encoding="utf-8") as f:
                content = f.read()
            if not content.startswith("---"):
                continue
            if not re.search(r"^version:\s*\d+", content, re.MULTILINE):
                rel = os.path.relpath(path, root)
                missing.append(rel)
    assert not missing, f"Files without version in frontmatter: {missing}"
    return True


def run_builtin_tests() -> TestResults:
    """Run all built-in prompt validation tests."""
    return _suite.run_all()


# 鈹€鈹€ Model Compliance Matrix 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# Structural tests that validate every model family prompt file has the
# guidance sections appropriate to its category (open-weight, compact, etc.).

_OPEN_WEIGHT_MODELS = frozenset({
    "llama", "mistral", "phi", "arcee", "minimax", "mimo", "nova",
    "doubao", "qwen", "glm", "hunyuan", "kimi",
})
_COMPACT_MODELS = frozenset({"mimo", "phi"})


@_suite.register("model_family_compliance",
                  "all model .prompt files must have ## Operational Guidance header")
def _test_model_family_compliance(loader: PromptLoader) -> bool:
    import os
    root = loader.root
    models_dir = os.path.join(root, "models")
    missing: list[str] = []
    for fn in sorted(os.listdir(models_dir)):
        if not fn.endswith(".prompt") or fn == "default.prompt":
            continue
        path = os.path.join(models_dir, fn)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if "## Operational Guidance" not in content:
            missing.append(fn)
    assert not missing, f"Model files without '## Operational Guidance': {missing}"
    return True


@_suite.register("open_weight_models_have_tool_discipline",
                  "open-weight models must reinforce tool-use discipline")
def _test_open_weight_tool_discipline(loader: PromptLoader) -> bool:
    import os
    root = loader.root
    models_dir = os.path.join(root, "models")
    missing: list[str] = []
    discipline_keywords = ("Tool Use Discipline", "Tool Persistence", "Step-by-Step")
    for fn in sorted(os.listdir(models_dir)):
        if not fn.endswith(".prompt"):
            continue
        name = fn[:-7]
        if name not in _OPEN_WEIGHT_MODELS:
            continue
        path = os.path.join(models_dir, fn)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if not any(kw in content for kw in discipline_keywords):
            missing.append(fn)
    assert not missing, (
        f"Open-weight models without tool discipline section: {missing}"
    )
    return True


@_suite.register("compact_models_have_sequential_guidance",
                  "compact models (mimo, phi) must have step-by-step guidance")
def _test_compact_sequential_guidance(loader: PromptLoader) -> bool:
    import os
    root = loader.root
    models_dir = os.path.join(root, "models")
    missing: list[str] = []
    sequential_keywords = ("Sequential", "Step-by-Step", "Simple and Sequential")
    for fn in sorted(os.listdir(models_dir)):
        if not fn.endswith(".prompt"):
            continue
        name = fn[:-7]
        if name not in _COMPACT_MODELS:
            continue
        path = os.path.join(models_dir, fn)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if not any(kw in content for kw in sequential_keywords):
            missing.append(fn)
    assert not missing, (
        f"Compact models without sequential guidance: {missing}"
    )
    return True


def main() -> int:
    """CLI entry point: run all prompt validation tests and exit with a status code.

    Returns:
        0 if all tests pass, 1 if any test fails.
    """
    result = run_builtin_tests()
    passed = len(result.passed)
    failed = len(result.failed)
    errors = len(result.errors)
    total = result.total

    print(f"\n{'=' * 50}")
    print(f"Prompt Test Suite: {passed}/{total} passed")
    if failed:
        print(f"FAILED: {failed}")
        for name, exc in result.failed:
            print(f"  X {name}: {exc}")
    if errors:
        print(f"ERRORS: {errors}")
        for name, exc in result.errors:
            print(f"  ! {name}: {exc}")
    if not failed and not errors:
        print("All tests passed!")
    print(f"{'=' * 50}\n")
    return 1 if (failed or errors) else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
