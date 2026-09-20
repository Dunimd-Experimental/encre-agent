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

"""Layered system-prompt builder.

Assembles the agent's system prompt from reusable *blocks*.  Each block has a
``priority`` (lower numbers are emitted earlier) and an optional ``condition``
list of intents that gate its inclusion.  :class:`EncrePromptBuilder` collects
core blocks, mode-specific blocks, slash-command blocks and a specialty block,
filters them by the active intents, sorts by priority, and concatenates them.

A ``__PROMPT_CACHE_BOUNDARY__`` marker is appended so callers can split the
static (cacheable) prefix from the dynamic (session-specific) suffix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from encre.prompts.loader import PromptLoader
from encre.utils.types import PermissionMode

_loader = PromptLoader()

# Valid permission-mode labels; anything else falls back to "default".
_PERMISSION_MODES = frozenset({
    "bypass", "dont_ask", "accept_edits", "plan", "spec", "auto", "default",
})

# Model-name substrings mapped to their family prompt file.  The first match
# (in insertion order) wins, so order matters: put more specific patterns
# before more general ones.  Unknown models fall back to ``default`` (an empty
# block), so no model ever receives guidance meant for a different family.
#
# IMPORTANT: the prompt *content* never names the model 鈥?only this selection
# logic uses the model name.  This keeps the identity-protection rule in
# identity.prompt ("never mention any model/provider name") intact: the model
# receives behavioural guidance without learning which family it belongs to.
_MODEL_FAMILY_PATTERNS: list[tuple[str, str]] = [
    # OpenAI / xAI family (codex is more specific than gpt)
    ("codex", "gpt"),
    ("gpt", "gpt"),
    ("grok", "gpt"),
    # Google family
    ("gemini", "gemini"),
    ("gemma", "gemini"),
    # Anthropic family
    ("claude", "claude"),
    ("anthropic", "claude"),
    # Chinese proprietary models
    ("glm", "glm"),
    ("qwen", "qwen"),
    ("deepseek", "deepseek"),
    ("kimi", "kimi"),
    ("hunyuan", "hunyuan"),
    ("hy3", "hunyuan"),
    ("minimax", "minimax"),
    ("doubao", "doubao"),
    # Open-weight models
    ("llama", "llama"),
    ("mistral", "mistral"),
    ("mixtral", "mistral"),
    ("mimo", "mimo"),
    # Other providers
    ("nova", "nova"),
    ("phi", "phi"),
    ("virtuoso", "arcee"),
    ("maestro", "arcee"),
    ("caller", "arcee"),
]

# 鈹€鈹€ Block definitions 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


@dataclass
class PromptBlock:
    """A single reusable fragment of the system prompt.

    Attributes:
        priority: Emission order; lower values come first.
        name: Unique block identifier (later blocks may override earlier ones).
        content: Rendered prompt text.
        condition: Intents that must be present for inclusion; ``None`` means
            always include.
    """

    priority: int
    name: str
    content: str
    condition: list[str] | None = None  # intents that trigger this block; None = always

    def with_context(self, ctx: dict[str, str]) -> PromptBlock:
        """Return a copy with ``{{key}}`` placeholders replaced by *ctx* values."""
        content = self.content
        for key, val in ctx.items():
            content = content.replace(f"{{{{{key}}}}}", val)
        return PromptBlock(
            priority=self.priority, name=self.name, content=content,
            condition=self.condition,
        )


# 鈹€鈹€ Core presets 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def _block_from_file(name: str, category: str = "blocks", **overrides: Any) -> PromptBlock:
    """Load a block from its ``.prompt`` file, reading metadata from frontmatter.

    The priority, name and condition are taken from the file's YAML frontmatter
    (see :func:`encre.prompts.loader._parse_frontmatter`), so the block's layout
    lives in one place 鈥?the file itself 鈥?rather than being duplicated in code.
    Explicit ``overrides`` (keyword args) take precedence over frontmatter for
    callers that need to special-case a block.
    """
    meta, body = _loader.load_full(name, category=category)
    return PromptBlock(
        priority=overrides.get("priority", meta.get("priority", 100)),
        name=overrides.get("name", meta.get("name", name)),
        condition=overrides.get("condition", meta.get("condition")),
        content=body,
    )


def _identity_block() -> PromptBlock:
    """Core identity/behaviour block, always included (highest priority 0)."""
    return _block_from_file("identity")


def _mandatory_constraints_block() -> PromptBlock:
    """Binding pre-action governance block: recall, clarify, checkpoint.

    Placed at priority 0.5 鈥?between identity (0) and task_completion (1) 鈥?
    so the mandatory constraints OVERRIDE the autonomy-enhancing guidance in
    task_completion/tool_execution before that guidance is even read.  This
    is the load-bearing block that prevents the model charging ahead on its
    own invented plan while the live user's real intent differs.
    """
    return _block_from_file("mandatory_constraints")


def _task_completion_block() -> PromptBlock:
    """Delivery-anchor block: finish the job, no stubs, no fabrication.

    Placed right after identity (priority 1) so the "deliver finished work"
    frame is established before any mode or tool guidance.  This is the
    single most important behavioural block 鈥?it counters the two universal
    failure modes: stopping after a stub, and fabricating output when a real
    path is blocked.
    """
    return _block_from_file("task_completion")


def _tool_execution_block() -> PromptBlock:
    """Tool-execution discipline: act-don't-describe, batch, verify, ground.

    Placed at priority 3 (after task_completion, before tool_usage) so the
    execution frame precedes the specific tool-selection guidance.  Complements
    ``tool_usage`` (which says *which* tools to use) with *how* to use them.
    """
    return _block_from_file("tool_execution")


def _post_execution_validation_block() -> PromptBlock:
    """Post-execution verification protocol: read-back, test, falsify.

    Placed at priority 4, immediately after tool_execution (3) and before
    tool_usage/safety (5), so the execution frame is immediately followed by
    the verification frame 鈥?"act, then verify" reads back-to-back.
    """
    return _block_from_file("post_execution_validation")


def _model_family_block(model: str = "") -> PromptBlock:
    """Model-family-specific operational guidance.

    Matches the model name against known families (GPT, Gemini, GLM, Qwen,
    DeepSeek, Claude) and injects the matching ``models/<family>.prompt``
    block.  Unknown models get an empty ``default`` block 鈥?no model ever
    receives guidance meant for a different family.

    Placed at priority 90 (after all core guidance, before the specialty
    block) so it acts as a modifier on top of the universal rules rather
    than replacing them.
    """
    model_lower = (model or "").lower()
    family = "default"
    for pattern, fam in _MODEL_FAMILY_PATTERNS:
        if pattern in model_lower:
            family = fam
            break
    content = _loader.load(family, category="models")
    return PromptBlock(priority=90, name="model_family", condition=None, content=content)


def _tool_usage_block(_tools: list[dict[str, Any]] | None = None) -> PromptBlock:
    """Tool-usage guidance block (the model's instructions for using tools)."""
    return _block_from_file("tool_usage")


def _permission_block(mode: PermissionMode) -> PromptBlock:
    """Permission/autonomy block describing how freely the agent may act.

    Each mode maps to a self-contained prompt file under
    ``permission/<mode>.prompt``; unknown modes fall back to ``default``.
    """
    mode_name = mode if mode in _PERMISSION_MODES else "default"
    content = _loader.load(mode_name, category="permission")
    return PromptBlock(priority=20, name="permission", condition=None, content=content)


def _language_block(lang_pref: str, app_lang: str) -> PromptBlock | None:
    """Language block forcing the response language (``zh``/``en``), or None."""
    resolved = lang_pref if lang_pref != "auto" else app_lang
    if resolved not in ("zh", "en"):
        return None
    content = _loader.load(resolved, category="language")
    return PromptBlock(priority=25, name="language", condition=None, content=content)


def _output_format_block() -> PromptBlock:
    """Output-formatting block (inverted pyramid, diff format, no emojis)."""
    return _block_from_file("output_format")


def _safety_block() -> PromptBlock:
    """Safety/security block (secrets, data protection, risk framework)."""
    return _block_from_file("safety")


def _task_management_block() -> PromptBlock:
    """Task-management block (todo lists) for coding/data sessions."""
    return _block_from_file("task_management")


def _memory_discipline_block() -> PromptBlock:
    """Memory-discipline block: declarative vs imperative, recall rules.

    Counters the failure mode where stored memory instructions override the
    live user's intent.  Always included 鈥?memory discipline applies to every
    session, not just coding/data.
    """
    return _block_from_file("memory_discipline")


def _specialty_coding_block() -> PromptBlock:
    """Specialty block for coding-domain guidance."""
    return _block_from_file("specialty_coding")


def _specialty_research_block() -> PromptBlock:
    """Specialty block for research-domain guidance."""
    return _block_from_file("specialty_research")


def _specialty_data_block() -> PromptBlock:
    """Specialty block for data-analysis-domain guidance."""
    return _block_from_file("specialty_data")


def _specialty_general_block() -> PromptBlock:
    """Fallback specialty block used when no specific intent is detected."""
    return _block_from_file("specialty_general")


def _iwork_block(workspace_root: str, workspace_name: str, project_summary: str = "") -> PromptBlock:
    """Workspace (iWork) mode block describing the project and its files."""
    ctx = dict(workspace_name=workspace_name, workspace_root=workspace_root)
    if project_summary:
        ctx["project_snapshot"] = f"\n\n### Project Snapshot\n{project_summary}"
    else:
        ctx["project_snapshot"] = ""
    return _block_from_file("workspace_mode").with_context(ctx)


def _plan_mode_block() -> PromptBlock:
    """Plan-mode block: instruct the model to plan, not execute."""
    return _block_from_file("plan_mode", category="modes")


def _spec_mode_block() -> PromptBlock:
    """Spec-mode block: instruct the model to specify, not implement."""
    return _block_from_file("spec_mode", category="modes")


def _command_instructions_block(name: str, body: str) -> PromptBlock:
    name = (name or "").strip()
    body = (body or "").strip()
    if not name:
        return PromptBlock(priority=190, name="command_instructions",
                           condition=None, content="")
    return _block_from_file("command_instructions").with_context(dict(
        command_name=name, command_body=body,
    ))


def _slash_commands_block(
    slash_command_mode: str, slash_commands: list[dict[str, Any]] | None,
    active_command_name: str = "",
) -> PromptBlock:
    """Inform the model about available slash commands and the active mode."""
    commands = slash_commands or []
    lines: list[str] = [_loader.load("header", category="slash_commands")]
    if slash_command_mode:
        lines.append(_loader.load_with_context(
            "mode_active", category="slash_commands", mode=slash_command_mode,
        ))
    else:
        # Normal mode is an explicit, declared state -- not "no mode".
        # Without this, the model can misread the internal "Work Phase"
        # (discover/execute/...) hint as the current mode and answer
        # e.g. "discover mode" when asked.
        lines.append(_loader.load("normal_mode", category="slash_commands"))
    if active_command_name:
        # A command may be active alongside (or instead of) a mode.  State
        # it explicitly so the model does not mistake the command's injected
        # instructions for a mode declaration.
        lines.append(_loader.load_with_context(
            "active_command", category="slash_commands",
            command_name=active_command_name,
        ))
    if commands:
        modes = [c for c in commands if c.get("kind") == "mode"]
        actions = [c for c in commands if c.get("kind", "action") != "mode"]
        if modes:
            lines.append(_loader.load("modes_header", category="slash_commands"))
            for cmd in modes:
                lines.append(_render_command_line(cmd))
        if actions:
            lines.append(_loader.load("actions_header", category="slash_commands"))
            for cmd in actions:
                lines.append(_render_command_line(cmd))
    else:
        lines.append(_loader.load("no_commands", category="slash_commands"))
    content = "\n".join(lines)
    return PromptBlock(priority=48, name="slash_commands", condition=None, content=content)


def _render_command_line(cmd: dict[str, Any]) -> str:
    """Render a single slash-command entry from the ``command_line`` template."""
    name = cmd.get("name", "")
    title = cmd.get("title", name)
    description = cmd.get("description", "")
    desc_suffix = f" -- {description}" if description else ""
    return _loader.load_with_context(
        "command_line", category="slash_commands",
        cmd_name=name, title=title, description_suffix=desc_suffix,
    )


def _skills_block(skill_summary: str = "") -> PromptBlock | None:
    """Dynamic skill catalogue: what skills are available and when to use them.

    Replaces the hard-coded skill lists previously baked into mode/specialty
    prompt blocks.  ``skill_summary`` is pre-rendered by the caller (the loop,
    from the live skill registry) so this module stays free of a registry
    dependency.  Returns ``None`` when no summary is provided.
    """
    if not skill_summary or not skill_summary.strip():
        return None
    return _block_from_file("skills_header").with_context(dict(
        skill_summary=skill_summary.strip(),
    ))


def _normal_mode_block(session_id: str = "") -> PromptBlock:
    """Normal (non-workspace) mode block with the session files directory."""
    from encre.capabilities.process import get_session_files_dir
    files_root = str(get_session_files_dir(session_id))
    return _block_from_file("general_mode").with_context(dict(files_root=files_root))


def _environment_block(workspace_root: str = "") -> PromptBlock:
    """Environment block: OS, shell hints, cwd, and git-repo detection."""
    import os as _os
    import platform as _platform
    import sys as _sys

    os_name = _platform.system() or _sys.platform
    if os_name == "Windows":
        details = f"Windows {_platform.version()} ({_platform.machine()})"
        shell_hint = _loader.load("shell_windows", category="environment")
    elif os_name == "Darwin":
        details = f"macOS {_platform.mac_ver()[0]} ({_platform.machine()})"
        shell_hint = _loader.load("shell_macos", category="environment")
    elif os_name == "Linux":
        details = f"Linux ({_platform.machine()})"
        shell_hint = _loader.load("shell_linux", category="environment")
    else:
        details = os_name
        shell_hint = ""

    cwd = workspace_root or _os.getcwd()
    is_git = _os.path.isdir(_os.path.join(cwd, ".git"))
    block = _block_from_file("environment")
    return block.with_context(dict(
        os_name=os_name, details=details, cwd=cwd,
        is_git="Yes" if is_git else "No", shell_hint=shell_hint,
    ))


def _current_datetime_block() -> PromptBlock:
    """Inject current date so the model has temporal awareness.
    The model's training data has a knowledge cutoff; this block explicitly
    overrides it with the real current date.

    Deliberately omits the clock time: a seconds-resolution timestamp inside
    the static prompt prefix would invalidate the provider-side prompt cache
    on every turn.  Date resolution is cache-friendly for a full day; exact
    time should be obtained via tools when it matters."""
    from datetime import datetime as _dt
    now = _dt.now()
    block = _block_from_file("datetime")
    return block.with_context(dict(
        date=now.strftime("%A, %B %d, %Y"),
        year=str(now.year),
    ))


# 鈹€鈹€ Builder 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


class EncrePromptBuilder:
    """Layered system prompt builder with priority-based block assembly."""

    def __init__(self) -> None:
        self._blocks: dict[str, PromptBlock] = {}

    def add_block(self, block: PromptBlock) -> None:
        """Register or override a block by its ``name``."""
        self._blocks[block.name] = block

    def remove_block(self, name: str) -> None:
        """Remove a previously registered block (no-op if absent)."""
        self._blocks.pop(name, None)

    def add_custom_instructions(self, text: str) -> None:
        """Add user-provided custom instructions at the highest priority."""
        self.add_block(PromptBlock(priority=200, name="custom", content=text))

    def build(
        self,
        mode: PermissionMode = "default",
        tools: list[dict[str, Any]] | None = None,
        specialty: str = "general",
        custom_instructions: str = "",
        intents: list[str] | None = None,
        workspace_root: str = "",
        workspace_name: str = "",
        project_summary: str = "",
        language_preference: str = "auto",
        app_language: str = "zh",
        session_id: str = "",
        slash_command_mode: str = "",
        slash_commands: list[dict[str, Any]] | None = None,
        skill_summary: str = "",
        active_command: dict[str, Any] | None = None,
        model: str = "",
    ) -> str:
        """Assemble the full system prompt from the active blocks.

        Collects core, mode, slash-command, skill-catalogue and specialty
        blocks, filters them by the active intents, sorts by priority,
        concatenates, and appends the prompt-cache boundary marker.
        """
        intents = intents or ["general"]

        # Collect blocks
        blocks: dict[str, PromptBlock] = dict(self._blocks)

        # Mode header -- iWork takes priority over normal
        if workspace_root:
            mode_block = _iwork_block(workspace_root, workspace_name or workspace_root, project_summary)
        else:
            mode_block = _normal_mode_block(session_id)
        blocks[mode_block.name] = mode_block

        # Always-add core blocks (if not overridden)
        defaults = [
            _identity_block(),
            _mandatory_constraints_block(),
            _task_completion_block(),
            _tool_execution_block(),
            _post_execution_validation_block(),
            _safety_block(),
            _current_datetime_block(),
            _environment_block(workspace_root),
            _tool_usage_block(tools),
            _task_management_block(),
            _memory_discipline_block(),
            _permission_block(mode),
            _language_block(language_preference, app_language),
            _output_format_block(),
            _model_family_block(model),
        ]
        for block in defaults:
            if block is not None and block.name not in blocks:
                blocks[block.name] = block

        # Slash command mode blocks -- inject detailed instructions when active.
        # The slash command mode is independent of the permission mode.
        if slash_command_mode == "plan" and "plan_mode" not in blocks:
            blocks["plan_mode"] = _plan_mode_block()
        elif slash_command_mode == "spec" and "spec_mode" not in blocks:
            blocks["spec_mode"] = _spec_mode_block()

        # Inform the model about available slash commands and the active mode.
        if "slash_commands" not in blocks:
            blocks["slash_commands"] = _slash_commands_block(
                slash_command_mode, slash_commands,
                active_command_name=(active_command or {}).get("name", ""),
            )

        # Active slash *command* (not a mode): sticky prompt injection,
        # re-applied every turn while active.  Explicitly framed so the
        # model never mistakes it for a mode declaration.
        if active_command and active_command.get("name") and "command_instructions" not in blocks:
            blocks["command_instructions"] = _command_instructions_block(
                active_command.get("name", ""),
                active_command.get("prompt", ""),
            )

        # Dynamic skill catalogue (replaces hard-coded skill lists in mode prompts).
        if "skills" not in blocks:
            skills_block = _skills_block(skill_summary)
            if skills_block is not None:
                blocks["skills"] = skills_block

        # Specialty block (if not overridden)
        if "specialty" not in blocks:
            specialty_map: dict[str, PromptBlock] = {}
            # A specialty can be triggered either by an active intent (derived
            # from the tool surface) or by an explicit ``specialty`` argument
            # (e.g. EncreCodingPrompt forces "coding").  Union both sources so
            # either path produces the right block.
            active = set(intents)
            if specialty:
                active.add(specialty)
            if "coding" in active:
                specialty_map["coding"] = _specialty_coding_block()
            if "research" in active:
                specialty_map["research"] = _specialty_research_block()
            if "data" in active:
                specialty_map["data"] = _specialty_data_block()
            # specific specialty takes priority, fall back to general
            if specialty in specialty_map:
                blocks["specialty"] = specialty_map[specialty]
            elif specialty_map:
                blocks["specialty"] = next(iter(specialty_map.values()))
            else:
                blocks["specialty"] = _specialty_general_block()

        # Custom instructions
        if custom_instructions:
            blocks["custom"] = PromptBlock(
                priority=200, name="custom", condition=None, content=custom_instructions,
            )

        # Filter by condition, then sort by priority, then assemble.
        # A block is kept when its condition is empty, or when any active intent
        # OR the explicit ``specialty`` argument matches the condition.  This
        # lets e.g. EncreCodingPrompt (which forces specialty="coding" with no
        # coding intent) still surface the coding specialty block.
        filter_keys = set(intents)
        if specialty:
            filter_keys.add(specialty)
        filtered: list[PromptBlock] = []
        for block in blocks.values():
            if block.condition is None or any(i in block.condition for i in filter_keys):
                filtered.append(block)

        sorted_blocks = sorted(filtered, key=lambda b: b.priority)
        parts: list[str] = []
        for block in sorted_blocks:
            content = block.content.strip()
            if content:
                parts.append(content)

        prompt = "\n\n".join(parts)

        # Prompt caching boundary: everything above is static/cacheable,
        # everything below is dynamic/session-specific.
        prompt += "\n\n" + _loader.load("cache_boundary", category="blocks") + "\n"

        return prompt

    def build_with_restrictions(
        self,
        mode: PermissionMode = "default",
        tools: list[dict[str, Any]] | None = None,
        specialty: str = "general",
        custom_instructions: str = "",
        intents: list[str] | None = None,
        workspace_root: str = "",
        workspace_name: str = "",
        project_summary: str = "",
        language_preference: str = "auto",
        app_language: str = "zh",
        session_id: str = "",
        slash_command_mode: str = "",
        slash_commands: list[dict[str, Any]] | None = None,
        skill_summary: str = "",
        active_command: dict[str, Any] | None = None,
        model: str = "",
    ) -> tuple[str, dict[str, Any]]:
        """Build the system prompt and return it with a restrictions metadata dict.

        The restrictions dict describes what constraints the caller should
        enforce at the code level (beyond what the prompt text instructs).
        """
        prompt = self.build(
            mode=mode, tools=tools, specialty=specialty,
            custom_instructions=custom_instructions, intents=intents,
            workspace_root=workspace_root, workspace_name=workspace_name,
            project_summary=project_summary,
            language_preference=language_preference,
            app_language=app_language, session_id=session_id,
            slash_command_mode=slash_command_mode,
            slash_commands=slash_commands,
            skill_summary=skill_summary, active_command=active_command,
            model=model,
        )

        # Determine which tools are restricted based on the active mode.
        restricted_tools: list[str] = []
        if slash_command_mode == "plan":
            restricted_tools = ["file_write", "file_edit", "write_file", "writeFile", "apply_patch", "bash"]
        elif slash_command_mode == "spec":
            restricted_tools = ["file_write", "file_edit", "write_file", "writeFile", "apply_patch", "bash"]

        # Bypass mode lifts safety re-ask prompts, but secrets/blast-radius
        # rules remain active at the prompt level.
        safety_level = "bypass" if mode == "bypass" else "normal"

        restrictions: dict[str, Any] = {
            "mode": slash_command_mode or "normal",
            "permission_mode": mode,
            "restricted_tools": restricted_tools,
            "safety_level": safety_level,
            "specialty": specialty,
            "intents": list(set(intents or ["general"])),
        }
        return prompt, restrictions

    def build_with_context(
        self,
        ctx: dict[str, str],
        mode: PermissionMode = "default",
        tools: list[dict[str, Any]] | None = None,
        specialty: str = "general",
    ) -> str:
        """Build with template variable substitution ({{key}} replaced by ctx values)."""
        prompt = self.build(mode=mode, tools=tools, specialty=specialty)
        for key, val in ctx.items():
            prompt = prompt.replace(f"{{{{{key}}}}}", val)
        return prompt
