#!/usr/bin/env python3

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

Assembles the agent's system prompt from reusable *blocks*.  Each block carries
a ``priority`` (lower numbers are emitted earlier) and up to two independent
gates:

* ``condition`` -- a list of *domain intents* (``coding`` / ``research`` /
  ``data``) that gate inclusion, i.e. which subject matter the block is about.
* ``registers`` -- the *response registers* (``chat`` / ``answer`` / ``work``)
  in which the section is in force, i.e. what shape the reply takes.  This is
  **activation metadata, not an emission filter**: every register-scoped
  section is emitted into the prompt on every build, because the system prompt
  is the session's cache-stable prefix -- switching sections in and out per
  turn would rewrite it and void the provider's prompt cache.  What the
  register does instead is decide, per turn, which of those sections the model
  is told to obey: the loop appends a short ``Live Register`` context block
  (see :func:`inactive_section_titles`) naming the active register and the
  sections that are inert right now.

:class:`EncrePromptBuilder` collects core blocks, the register sections, mode-specific
blocks, slash-command blocks and a specialty block, filters them by the active
intents, sorts by priority, and concatenates them.  The result is deliberately
independent of the current turn's response register so the prefix stays
cacheable across a whole session.

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
_PERMISSION_MODES = frozenset(
    {
        "bypass",
        "dont_ask",
        "accept_edits",
        "plan",
        "spec",
        "auto",
        "default",
    }
)

#: The three response registers, in the order they are explained to the model.
#: An unrecognised label falls back to ``work`` -- the strictest frame -- so a
#: mis-classified turn still delivers rather than chats.
REGISTER_ORDER: tuple[str, ...] = ("chat", "answer", "work")

#: One-line label per register, rendered into the per-turn live declaration.
REGISTER_TITLES: dict[str, str] = {
    "chat": "CHAT — the conversation itself is the deliverable",
    "answer": "ANSWER — explain or report, grounded, nothing to produce",
    "work": "WORK — produce the finished result the user asked for",
}

# Model-name substrings mapped to their family prompt file.  The first match
# (in insertion order) wins, so order matters: put more specific patterns
# before more general ones.  Unknown models fall back to ``default`` (an empty
# block), so no model ever receives guidance meant for a different family.
#
# IMPORTANT: the prompt *content* never names the model — only this selection
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

# Harmonization clause prepended to every non-empty model-family block.  The
# family files carry compensation only; this states what they may never do.
_MODEL_FAMILY_HARMONIZATION = (
    "## Operational Guidance Boundaries\n"
    "The notes in this section adjust *how* you apply the rules you are already "
    "given, for this family's known tendencies. They never add a stopping rule, "
    "never change when you ask the user, and never relax or tighten the live "
    "response register. Where a note here appears to say more than the rules "
    "currently around it, follow the rules currently around it."
)

# ── Block definitions ──────────────────────────────────────────────


@dataclass
class PromptBlock:
    """A single reusable fragment of the system prompt.

    Attributes:
        priority: Emission order; lower values come first.
        name: Unique block identifier (later blocks may override earlier ones).
        content: Rendered prompt text.
        condition: Intents that must be present for inclusion; ``None`` means
            always include.

    Register scoping is NOT a field here: every register section is always
    emitted (see :func:`_register_blocks`), and which ones are in force this
    turn is declared per turn from the ``registers:`` frontmatter -- see
    :func:`register_activation_map`.
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
            priority=self.priority,
            name=self.name,
            content=content,
            condition=self.condition,
        )


# ── Core presets ────────────────────────────────────────────────────


def _block_from_file(name: str, category: str = "blocks", **overrides: Any) -> PromptBlock:
    """Load a block from its ``.prompt`` file, reading metadata from frontmatter.

    The priority, name and condition are taken from the file's YAML
    frontmatter (see :func:`encre.prompts.loader._parse_frontmatter`), so the
    block's layout lives in one place — the file itself — rather than being
    duplicated in code.  Explicit ``overrides`` (keyword args) take precedence
    over frontmatter for callers that need to special-case a block.
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
    """Binding pre-action governance: recall, clarify, disclose, precedence, rules.

    Placed at priority 0.5 — between identity (0) and task_completion (1) — so
    the gates qualify the autonomy-enhancing guidance in the delivery and
    execution blocks before that guidance is even read.  It carries no
    pause/continue policy: how long an unattended chain runs is decided by the
    checkpoint gate in :mod:`encre.loop_stability`, which says so when it
    applies.
    """
    return _block_from_file("mandatory_constraints")


def _register_policy_block() -> PromptBlock:
    """Always-on declaration of the three response registers.

    The single place where the register vocabulary lives.  It also explains the
    architecture the model has to work inside: every register section is
    present in this prompt at all times (so the cached prefix never changes
    mid-session), and the ``Live Register`` declaration in the context updates
    states which one governs this turn.
    """
    return _block_from_file("register_policy")


def _register_blocks() -> list[PromptBlock]:
    """All three register sections, always emitted into the frozen prefix.

    Emission is deliberately NOT conditioned on the current register: the
    system prompt is the cache-stable prefix, so it is built once per session
    and stays byte-identical while the turn's register changes.  Which of these
    sections is actually in force is declared per turn -- see
    :func:`register_activation_map` and the ``Live Register`` context block the
    loop injects.
    """
    return [_block_from_file(f"register_{name}") for name in REGISTER_ORDER]


def register_activation_map() -> dict[str, list[str]]:
    """Map section title -> the registers in which that section is in force.

    Read straight from the ``registers:`` frontmatter of every block file, so
    the activation table cannot drift from the sections it describes: adding a
    ``registers:`` key to a block is the only edit needed to make the live
    register declaration mention it.
    """
    import os
    import re

    heading_re = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)
    blocks_dir = os.path.join(_loader.root, "blocks")
    out: dict[str, list[str]] = {}
    for filename in sorted(os.listdir(blocks_dir)):
        if not filename.endswith(".prompt"):
            continue
        meta, body = _loader.load_full(filename[: -len(".prompt")], category="blocks")
        registers = meta.get("registers")
        if not registers:
            continue
        match = heading_re.search(body)
        title = (match.group(1) if match else filename).split("—")[0].split("--")[0].strip()
        out[title] = [str(register) for register in registers]
    return out


def inactive_section_titles(register: str) -> list[str]:
    """Titles of the register-scoped sections that are inert in *register*.

    Powers the per-turn ``Live Register`` declaration: naming what is switched
    off is what stops a work-shaped section bleeding into a greeting, without
    having to remove it from the cached prefix.
    """
    resolved = register if register in REGISTER_ORDER else "work"
    return sorted(title for title, registers in register_activation_map().items() if resolved not in registers)


def _task_completion_block() -> PromptBlock:
    """Delivery-anchor block: finish the job, no stubs, no fabrication.

    Placed at priority 1 so the "deliver finished work" frame is established
    before any mode or tool guidance.  This is the most important behavioural
    block for the ``work`` register -- it counters the two universal failure
    modes: stopping after a stub, and fabricating output when a real path is
    blocked.  It is register-gated to ``work``: in conversation the same text
    would turn a greeting into a deliverable.
    """
    return _block_from_file("task_completion")


def _tool_execution_block() -> PromptBlock:
    """Tool-execution discipline: act-don't-describe, batch, verify, ground.

    Placed at priority 4 (after the catalog and usage guidance, before
    verification) so the execution frame precedes the check that closes it.
    Complements ``tool_usage`` (which says *which* tools to use) with *how* to
    use them.  Register-gated: in ``chat`` there is nothing to execute.
    """
    return _block_from_file("tool_execution")


def _post_execution_validation_block() -> PromptBlock:
    """Post-execution verification protocol: read-back, test, falsify.

    Placed at priority 4.5, immediately after tool_execution (4), so "act, then
    verify" reads back-to-back.  Register-gated to ``work``: an explanation has
    no artefact to read back.
    """
    return _block_from_file("post_execution_validation")


def _model_family_block(model: str = "") -> PromptBlock:
    """Model-family-specific operational guidance.

    Matches the model name against known families (GPT, Gemini, GLM, Qwen,
    DeepSeek, Claude) and injects the matching ``models/<family>.prompt``
    block.  Unknown models get an empty ``default`` block — no model ever
    receives guidance meant for a different family.

    Every family file carries *only* family-specific compensation.  The
    harmonization clause below is prepended in code -- one place, for all
    families -- because the per-file copies of it used to contradict each
    other (one family's file said "act, don't ask", another's said "ask
    rather than guess", both at the same layer as the Clarification Gate).

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
    content = _loader.load(family, category="models").strip()
    if content:
        content = f"{_MODEL_FAMILY_HARMONIZATION}\n\n{content}"
    return PromptBlock(priority=90, name="model_family", condition=None, content=content)


def _apply_model_profile(blocks: dict[str, PromptBlock], profile: Any) -> None:
    """Apply a resolved ModelProfile to the assembled block dict, in place.

    Drops the heavy behavioural blocks the profile deems too costly for a
    weak model, swaps slimmed replacements for the ones that must stay
    (``blocks/slim_<name>.prompt``), and injects the profile's short
    behavioural preamble.  Profiles without prompt fields are a no-op, so
    standard/strong tiers emit byte-identical prompts to before.
    """
    for name in getattr(profile, "drop_prompt_blocks", ()) or ():
        blocks.pop(name, None)
    for name in getattr(profile, "slim_prompt_blocks", ()) or ():
        block = blocks.get(name)
        if block is None:
            continue
        try:
            slim = _loader.load(f"slim_{name}", category="blocks")
        except Exception:
            slim = ""
        if slim.strip():
            blocks[name] = PromptBlock(
                priority=block.priority,
                name=block.name,
                content=slim,
                condition=block.condition,
            )
    gain = (getattr(profile, "prompt_gain", "") or "").strip()
    if gain:
        blocks["model_profile_gain"] = PromptBlock(
            priority=3.9,
            name="model_profile_gain",
            condition=None,
            content=gain,
        )


def _tool_catalog_block(tools: list[dict[str, Any]] | None = None) -> PromptBlock:
    """Dynamic "Available Tools" catalog rendered from the live tool payload.

    The loop passes the exact tool list sent to the model (openai ``tools``
    format), so this block can never drift from what the model can actually
    call.  With register-as-activation every listed tool is active; there is
    no deferred zone.  Returns an empty-content block when no tools are
    provided (e.g. sub-agents built without a tool surface) so the block
    vanishes instead of printing a stale list.

    Emitted at priority 3 -- deliberately BEFORE ``tool_usage`` (3.5), which
    refers to "the Available Tools list above".  In force in every register:
    knowing what exists is harmless in conversation, and a chat turn still
    needs to be able to check a fact instead of guessing it.
    """
    lines: list[str] = []
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        name = (fn or tool or {}).get("name", "") if isinstance(tool, dict) else ""
        desc = (fn or tool or {}).get("description", "") if isinstance(tool, dict) else ""
        if not name:
            continue
        desc = " ".join(str(desc).split())
        if len(desc) > 160:
            desc = desc[:157] + "..."
        lines.append(f"- `{name}` — {desc}" if desc else f"- `{name}`")
    if not lines:
        return PromptBlock(priority=3, name="tool_catalog", content="")
    content = _loader.load_with_context("tool_instructions", tools_list="\n".join(lines))
    return PromptBlock(priority=3, name="tool_catalog", content=content)


def _tool_usage_block(tools: list[dict[str, Any]] | None = None) -> PromptBlock:
    """Tool-usage guidance block (how to choose and combine tools).

    The static guidance deliberately avoids naming plugin tools: which tools
    exist is the job of the dynamic ``tool_catalog`` block rendered from the
    live payload.
    """
    del tools  # guidance is static; the catalog block carries the live list
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
    live user's intent.  Always included — memory discipline applies to every
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


def select_specialty_block(specialty: str | None, intents: list[str] | None = None) -> PromptBlock:
    """Pick the domain-specialty section for a turn.

    A specialty can be triggered either by an active intent (derived from the
    tool surface) or by an explicit ``specialty`` argument (e.g.
    EncreCodingPrompt forces ``"coding"``); both sources are unioned so either
    path produces the right section.  A specific specialty outranks the general
    fallback.
    """
    active = set(intents or [])
    if specialty:
        active.add(specialty)
    specialty_map: dict[str, PromptBlock] = {}
    if "coding" in active:
        specialty_map["coding"] = _specialty_coding_block()
    if "research" in active:
        specialty_map["research"] = _specialty_research_block()
    if "data" in active:
        specialty_map["data"] = _specialty_data_block()
    if specialty in specialty_map:
        return specialty_map[specialty]
    if specialty_map:
        return next(iter(specialty_map.values()))
    return _specialty_general_block()


def specialty_block_text(specialty: str | None, intents: list[str] | None = None) -> str:
    """Rendered text of the domain-specialty section for this turn.

    The loop supplies this as a per-turn context block (see
    :func:`select_specialty_block`), so a topic change reaches the model
    without rewriting the cached prefix.
    """
    return select_specialty_block(specialty, intents).content.strip()


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
        return PromptBlock(priority=190, name="command_instructions", condition=None, content="")
    return _block_from_file("command_instructions").with_context(
        dict(
            command_name=name,
            command_body=body,
        )
    )


def _slash_commands_block(
    slash_command_mode: str,
    slash_commands: list[dict[str, Any]] | None,
    active_command_name: str = "",
) -> PromptBlock:
    """Inform the model about available slash commands and the active mode."""
    commands = slash_commands or []
    lines: list[str] = [_loader.load("header", category="slash_commands")]
    if slash_command_mode:
        lines.append(
            _loader.load_with_context(
                "mode_active",
                category="slash_commands",
                mode=slash_command_mode,
            )
        )
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
        lines.append(
            _loader.load_with_context(
                "active_command",
                category="slash_commands",
                command_name=active_command_name,
            )
        )
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
        "command_line",
        category="slash_commands",
        cmd_name=name,
        title=title,
        description_suffix=desc_suffix,
    )


def skills_block(skill_summary: str = "") -> PromptBlock | None:
    """Dynamic skill catalogue: what skills are available and when to use them.

    Replaces the hard-coded skill lists previously baked into mode/specialty
    prompt blocks.  ``skill_summary`` is pre-rendered by the caller (the loop,
    from the live skill registry) so this module stays free of a registry
    dependency.  Returns ``None`` when no summary is provided.
    """
    if not skill_summary or not skill_summary.strip():
        return None
    return _block_from_file("skills_header").with_context(
        dict(
            skill_summary=skill_summary.strip(),
        )
    )


def _session_files_root(session_id: str = "") -> str:
    """The per-session directory user-created files belong to (general mode)."""
    from encre.capabilities.process import get_session_files_dir

    return str(get_session_files_dir(session_id))


def _normal_mode_block(session_id: str = "", files_root: str = "") -> PromptBlock:
    """Normal (non-workspace) mode block with the session files directory."""
    return _block_from_file("general_mode").with_context(dict(files_root=files_root or _session_files_root(session_id)))


def _environment_block(workspace_root: str = "", session_id: str = "") -> PromptBlock:
    """Environment block: OS, shell hints, and the directory this session owns.

    General mode no longer labels the app's launch directory as a "working
    directory".  That line was load-bearing for a real bug: relative writes
    already land in the session files directory, but absolute paths pass
    through by design, so telling the model "Working directory: D:\\..." was
    enough for it to start emitting absolute paths under whatever folder the
    client happened to be started from -- files landing in the app's own tree
    instead of the session's.  The launch directory is still named, because a
    shell call really does inherit it, but as a read-only fact with an
    explicit "not an output target" rule attached.
    """
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

    launch_dir = _os.getcwd()
    lines: list[str] = []
    if workspace_root:
        is_git = _os.path.isdir(_os.path.join(workspace_root, ".git"))
        lines.append(f"Project workspace: `{workspace_root}`")
        lines.append(f"Working directory: {workspace_root}")
        lines.append(f"Is directory a git repo: {'Yes' if is_git else 'No'}")
    else:
        lines.append("Project workspace: none -- this session is not bound to a project directory.")
        lines.append(
            "Output location: the session files directory named in the Session Environment "
            "section. Anything you create goes there unless the user names another path."
        )
        lines.append(
            f"App launch directory: `{launch_dir}` -- this is where the client process was "
            "started, NOT a working directory. Do not write files, create folders, scaffold or "
            "run builds there. A shell command inherits it only when you pass no ``cwd``; give "
            "``cwd`` explicitly whenever a command needs a directory."
        )
        lines.append("Is directory a git repo: n/a (no workspace bound)")

    block = _block_from_file("environment")
    return block.with_context(
        dict(
            os_name=os_name,
            details=details,
            environment_lines="\n".join(lines),
            shell_hint=shell_hint,
        )
    )


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
    return block.with_context(
        dict(
            date=now.strftime("%A, %B %d, %Y"),
            year=str(now.year),
        )
    )


# ── Builder ─────────────────────────────────────────────────────────


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
        model_profile: Any = None,
        include_specialty: bool = True,
    ) -> str:
        """Assemble the full system prompt from the active blocks.

        Collects core, register, mode, slash-command, skill-catalogue and
        specialty blocks, filters them by the active intents, sorts by
        priority, concatenates, and appends the prompt-cache boundary marker.

        The output carries the sections of ALL three response registers and is
        therefore identical whatever this turn's register turns out to be --
        that is what keeps the session prefix byte-stable and the provider
        prompt cache alive.  Which sections apply is declared per turn by the
        ``Live Register`` context block the loop appends after the boundary.

        ``model_profile`` (a resolved
        :class:`~encre.backends.model_profiles.ModelProfile`) optionally
        degrades the prompt load for weak models; ``None`` keeps the
        historical full behaviour.
        """
        intents = intents or ["general"]

        # Collect blocks
        blocks: dict[str, PromptBlock] = dict(self._blocks)

        # Mode header -- iWork takes priority over normal.  In general mode the
        # Environment section names the session files directory too, derived by
        # the same helper, so the two sections can never disagree about where
        # output belongs.
        if workspace_root:
            mode_block = _iwork_block(workspace_root, workspace_name or workspace_root, project_summary)
        else:
            mode_block = _normal_mode_block(session_id)
        blocks[mode_block.name] = mode_block

        # Register layer: the shared vocabulary plus all three register
        # sections.  Emitting all of them (rather than the live one) is the
        # cache decision; ``registers:`` frontmatter then tells the per-turn
        # declaration which sections are in force right now.
        blocks["register_policy"] = _register_policy_block()
        for register_block in _register_blocks():
            blocks[register_block.name] = register_block

        # Always-add core blocks (if not overridden).  Sorted by priority at
        # build time; the order below is the intended emission order:
        # identity -> register vocabulary -> the three register sections ->
        # binding gates -> memory governance -> delivery standard -> tool
        # catalog (what exists) -> tool usage (which to use) -> execution
        # discipline (how) -> verification (prove it).
        defaults = [
            _identity_block(),  # 0
            _mandatory_constraints_block(),  # 0.5
            _memory_discipline_block(),  # 0.6
            _task_completion_block(),  # 1
            _tool_catalog_block(tools),  # 3
            _tool_usage_block(tools),  # 3.5
            _tool_execution_block(),  # 4
            _post_execution_validation_block(),  # 4.5
            _safety_block(),
            _current_datetime_block(),
            _environment_block(workspace_root, session_id),
            _task_management_block(),
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
                slash_command_mode,
                slash_commands,
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
            catalogue_block = skills_block(skill_summary)
            if catalogue_block is not None:
                blocks["skills"] = catalogue_block

        # Domain specialty.  ``include_specialty=False`` means the caller supplies
        # it per turn as a context block -- which the loop does: the specialty
        # section is the one part of the prompt that tracks what the user is
        # asking *about*, and locking it into the frozen prefix either kept a
        # session answering with the first message's domain guidance, or forced a
        # full prefix rebuild on every topic change.
        if include_specialty and "specialty" not in blocks:
            blocks["specialty"] = select_specialty_block(specialty, intents)

        # Custom instructions
        if custom_instructions:
            blocks["custom"] = PromptBlock(
                priority=200,
                name="custom",
                condition=None,
                content=custom_instructions,
            )

        # Per-model behaviour load (drop/slim blocks for weak models).
        # Applied after every block has been registered so drops cannot be
        # silently re-added by the later sections.
        if model_profile is not None:
            _apply_model_profile(blocks, model_profile)

        # Filter by the domain gate, then sort by priority, then assemble.  A
        # block is kept when its condition is empty, or when any active intent
        # OR the explicit ``specialty`` argument matches it.  This lets e.g.
        # EncreCodingPrompt (which forces specialty="coding" with no coding
        # intent) still surface the coding specialty block.  Note there is no
        # register gate here on purpose: register sections are all emitted and
        # activated per turn instead, so the prefix never changes shape.
        filter_keys = set(intents)
        if specialty:
            filter_keys.add(specialty)
        filtered: list[PromptBlock] = []
        for block in blocks.values():
            if block.condition is not None and not any(i in block.condition for i in filter_keys):
                continue
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
        model_profile: Any = None,
        include_specialty: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        """Build the system prompt and return it with a restrictions metadata dict.

        The restrictions dict describes what constraints the caller should
        enforce at the code level (beyond what the prompt text instructs).
        """
        prompt = self.build(
            mode=mode,
            tools=tools,
            specialty=specialty,
            custom_instructions=custom_instructions,
            intents=intents,
            workspace_root=workspace_root,
            workspace_name=workspace_name,
            project_summary=project_summary,
            language_preference=language_preference,
            app_language=app_language,
            session_id=session_id,
            slash_command_mode=slash_command_mode,
            slash_commands=slash_commands,
            skill_summary=skill_summary,
            active_command=active_command,
            model=model,
            model_profile=model_profile,
            include_specialty=include_specialty,
        )

        # Determine which tools are restricted based on the active mode.
        restricted_tools: list[str] = []
        if slash_command_mode == "plan" or slash_command_mode == "spec":
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
