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

"""Prompt phase: build (and cache) the per-run system prompt.

Extracted verbatim from the original monolithic ``_run_impl`` preparation
section.  ``_build_turn_system_prompt`` classifies intent, activates
``/skill`` invocations, assembles every enrichment block (workspace,
codebase index, architecture contract, Plan-Do-Review, memory, profile,
soul, meta-cognition, device context, stage / working-set / turn-summary /
stuck-recovery, user rules, context annotation) and updates the session's
system + user messages.
"""

import contextlib
import time
from typing import Any

from encre.events.lifecycle import SessionStarted, UserMessagePersisted
from encre.logging_config import get_logger
from encre.loop.phase_api import (
    LoopPhase,
    PhaseContext,
    PhaseResult,
    PhaseScope,
)
from encre.loop.turn_ctx import TurnContext
from encre.loop_stability import _detect_requirement_change, build_standing_orders_reminder
from encre.prompts.classifier import classify_intents

logger = get_logger(__name__)


class PhasePromptMixin:
    """System-prompt construction for one ``run()`` invocation."""

    async def _build_turn_system_prompt(self, t: TurnContext) -> None:
        """Build the system prompt and prepare session messages.

        Sets ``t.system_prompt`` (fully assembled), ``t.intents``,
        ``t.skip_enrichment``, ``t.skill_prompt`` and ``t.context_msgs``.

        Args:
            t: The shared turn context.

        Returns:
            None.
        """
        # Classify user intent for dynamic prompt assembly
        intents = classify_intents(t.prompt)
        t.intents = intents

        # Detect mid-conversation requirement changes: when the user's
        # latest message signals a shift in direction, invalidate the
        # cached user requirements summary so the next compact produces
        # a fresh one.  This prevents the model from anchoring to stale
        # requirements after the user says "actually, let's do X instead".
        _detected_change = _detect_requirement_change(t.prompt)
        if _detected_change and self._state_mgr.user_requirements_summary:
            logger.info("[run] detected requirement change: %s -- clearing cached summary", _detected_change)
            self._state_mgr.user_requirements_summary = ""

        # Plan-Do-Review (WORKSPACE profile, top-level turn): build a step
        # graph from a complex task before execution so the coder is anchored
        # to a written plan.  Re-initialise when the user redirects mid-task
        # (detected requirement change) or when no plan exists yet.
        if (
            self._profile.workspace_delegation
            and self.sub_agent_depth == 0
            and t.prompt
            and self._pdr.should_plan(t.prompt)
        ):
            if not self._pdr_active or _detected_change:
                self._pdr.initialize(t.prompt)
                self._pdr_active = True
                self._pdr.start_next_step()
                logger.info("[run] Plan-Do-Review initialized for task (steps=%d)", len(self._pdr.plan.steps))

        # Activate any skills invoked via /skill-name syntax
        skill_prompt, prompt = await self._activate_skills(t.prompt)
        t.skill_prompt = skill_prompt
        t.prompt = prompt
        system_prompt = t.system_prompt
        custom_instructions = t.custom_instructions
        slash_command_mode = t.slash_command_mode
        slash_commands = t.slash_commands
        _t0 = time.time()
        tools = None
        if self.backend.supports_tool_calling():
            # Discovery handles ToolSet resolution + find_tool + MCP merging.
            # Expand the base toolset with intent-matched sets (coding /
            # research / data) so advanced tools are reachable without the
            # model having to self-select find_tool first.
            self._tool_set_name = self._resolve_tool_set_for_mode(intents)
            self.discovery.tool_set_name = self._tool_set_name
            tools = self.discovery.get_active_tools_payload(self.session.id, fmt="openai")
        ws_root, ws_name, ws_summary = self._ctx_bldr.workspace_info()

        if system_prompt is None:
            # Cache the base system prompt by a content-hash key so we don't
            # rebuild it every turn when nothing changed.
            _cache_key = (
                self.config.permission_mode,
                self.config.slash_command_mode,
                self.session.id,
                tuple(t.get("function", {}).get("name", "") for t in tools) if tools else (),
                tuple(sorted(intents)),
                ws_root, ws_name, ws_summary,
                self.config.language_preference,
                self.config.language,
                custom_instructions,
                tuple(c.get("name", "") for c in (slash_commands or [])),
                tuple(sorted(s.name for s in self.skill_registry.list_all()
                       if s.user_invocable and not s.name.startswith("tool-"))) if self.skill_registry else (),
                self.active_command_name,
                _active_cmd_full := (self.config.active_command or {}).get("prompt", ""),
                self.config.model,
            )
            if self._sys_prompt_cache is not None and self._sys_prompt_cache_key == _cache_key:
                system_prompt = self._sys_prompt_cache
            else:
                system_prompt = self.prompt_builder.build_system_prompt(
                    self.config.permission_mode,
                    tools=tools,
                    intents=intents,
                    workspace_root=ws_root,
                    workspace_name=ws_name,
                    project_summary=ws_summary,
                    language_preference=self.config.language_preference,
                    app_language=self.config.language,
                    custom_instructions=custom_instructions,
                    session_id=self.session.id,
                    slash_command_mode=slash_command_mode,
                    slash_commands=slash_commands,
                    skill_summary=self._render_skill_catalogue(),
                    active_command=getattr(self.config, "active_command", None),
                    model=self.config.model,
                )
                self._sys_prompt_cache = system_prompt
                self._sys_prompt_cache_key = _cache_key
        elif slash_command_mode in ("plan", "spec"):
            # Custom system_prompt was provided (e.g., from an active agent).
            # Plan/spec mode requires mode-specific instructions -- build the
            # full mode-aware prompt and prepend the custom content so both
            # the custom prompt and the mode instructions are in effect.
            built = self.prompt_builder.build_system_prompt(
                self.config.permission_mode,
                tools=tools,
                intents=intents,
                workspace_root=ws_root,
                workspace_name=ws_name,
                project_summary=ws_summary,
                language_preference=self.config.language_preference,
                app_language=self.config.language,
                custom_instructions=custom_instructions,
                session_id=self.session.id,
                slash_command_mode=slash_command_mode,
                slash_commands=slash_commands,
                skill_summary=self._render_skill_catalogue(),
                active_command=getattr(self.config, "active_command", None),
                model=self.config.model,
            )
            system_prompt = system_prompt + "\n\n" + built
        else:
            # Custom system_prompt provided but we are in normal mode.  The
            # custom prompt may be silent about the current mode, so the model
            # can infer it incorrectly from earlier plan/spec messages.  Force
            # an explicit normal-mode declaration at the top.
            system_prompt = "Current mode: NORMAL MODE. You are not in plan mode or spec mode. Ignore any mode claims in earlier messages; this system instruction is authoritative.\n\n" + system_prompt

        # When a custom system_prompt was provided by a parent agent (not
        # None, not plan/spec mode), skip workspace context enrichment.
        # However, when no custom prompt was given (system_prompt was None),
        # the agent runs as a full session -- don't skip enrichments.
        _original_system_prompt_was_none = system_prompt is None
        _skip_enrichment = (
            system_prompt is not None
            and slash_command_mode not in ("plan", "spec")
            and not _original_system_prompt_was_none
        )
        t.skip_enrichment = _skip_enrichment

        # Inject the per-mode behaviour preamble (GENERAL / WORKSPACE /
        # AUTOMATION) so the model actually feels the mode difference, not
        # just the internal tuning knobs.  Recorded under "Mode" so the
        # context annotation tracks it across turns.
        _mode_gain = getattr(self._profile, "prompt_gain", "") or ""
        if not _skip_enrichment and _mode_gain:
            self._ctx_renderer.record("Mode", _mode_gain)
            system_prompt = system_prompt + "\n\n" + _mode_gain

        # Inject codebase index context (multi-language code search + dependencies)
        if not _skip_enrichment:
            codebase_ctx = await self._ctx_bldr.build_codebase_context()
            if codebase_ctx:
                self._ctx_renderer.record("Codebase Index", codebase_ctx)
                system_prompt = system_prompt + "\n\n" + codebase_ctx

        # Inject the architecture contract artifact (workspace mode): design
        # decisions agreed by the planner/architect role are BINDING for
        # implementation.  Re-injected every turn so the coder stays anchored
        # to the agreed architecture instead of drifting across a long task.
        if not _skip_enrichment and self._profile.contract_inject:
            contract_ctx = self._ctx_bldr.build_architecture_contract()
            if contract_ctx:
                self._ctx_renderer.record("Architecture Contract", contract_ctx)
                system_prompt = system_prompt + "\n\n" + contract_ctx

        # Inject Plan-Do-Review progress (WORKSPACE profile): the current step,
        # success criteria, and progress summary keep the coder anchored to the
        # written plan across turns (mirrors Claude Code's plan-do-review).
        if not _skip_enrichment and self._pdr_active:
            pdr_ctx = self._pdr.get_context()
            if pdr_ctx:
                self._ctx_renderer.record("Plan-Do-Review", pdr_ctx)
                system_prompt = system_prompt + "\n\n" + pdr_ctx

        # Append skill/tool-skill/doc-skill prompts AFTER the base prompt so
        # the static cacheable prefix (everything above
        # __PROMPT_CACHE_BOUNDARY__) stays byte-identical across turns.
        # Prepending them would change the first system message every turn
        # and cause 100% prompt-cache misses on OpenAI/DeepSeek/Anthropic.
        # Relative order (doc -> tool -> skill) matches the previous prepend
        # order so downstream content reads the same.
        doc_skills_prompt = self._render_active_doc_skills()
        if doc_skills_prompt:
            system_prompt = system_prompt + "\n\n" + doc_skills_prompt

        tool_skills_prompt = self._render_active_tool_skills()
        if tool_skills_prompt:
            system_prompt = system_prompt + "\n\n" + tool_skills_prompt

        if skill_prompt:
            system_prompt = system_prompt + "\n\n" + skill_prompt

        # Inject user requirements summary: a compact description of the
        # user's core goals extracted from the last compact summary.  Lives
        # in session metadata so it survives compaction.  Only for the main
        # agent (sub-agents get their own self-contained brief).
        if not _skip_enrichment:
            _req_summary = self._state_mgr.user_requirements_summary
            if _req_summary:
                system_prompt = system_prompt + "\n\n" + _req_summary
            # P5: coordinator-style delegation guidance.  Steers the main
            # agent toward good delegation hygiene (understand -> delegate
            # self-contained briefs -> synthesise) on complex multi-step
            # work.  Mirrors Claude Code's coordinatorMode.ts.
            system_prompt = system_prompt + "\n\n" + build_delegation_guidance()

        if _skip_enrichment:
            # Sub-agent behavioral framework: essential blocks every agent
            # needs (tool protocol, safety, identity, output format) but
            # WITHOUT workspace-specific context that would distract from
            # the delegated task.
            from encre.prompts.loader import PromptLoader
            _loader = PromptLoader()
            _behavioral_parts: list[str] = []
            for _bname in ("identity", "safety", "tool_usage", "output_format"):
                try:
                    _bcontent = _loader.load(_bname)
                    if _bcontent:
                        _behavioral_parts.append(_bcontent)
                except Exception:
                    pass
            # Sub-agent identity and depth guard. When this loop is itself
            # a delegated sub-agent (depth > 0), tell the model that and
            # forbid further recursion. This blocks infinite agent-of-agent
            # chains and keeps sub-agents focused on the delegated task.
            if self.sub_agent_depth > 0:
                _behavioral_parts.append(
                    "## Sub-Agent Identity\n"
                    f"You are a delegated sub-agent (depth {self.sub_agent_depth}). "
                    "You were spawned by a parent agent to perform a specific task. "
                    "Your output is returned to the parent. Do NOT spawn further sub-agents -- "
                    "the runtime forbids two levels of nesting. Complete the assigned task with "
                    "the tools available and return a concise final answer. If you need to "
                    "parallelize work, return a list of sub-tasks to the parent instead."
                )
            # Language preference -- NOTE: sub-agents always think and respond
            # in English (enforced by sub_agent_enforcement.prompt) for reliable
            # state matching and output parsing. The parent agent handles
            # translation when relaying results to the user. Do NOT inject
            # non-English language preferences here -- it conflicts with the
            # English-only enforcement and breaks output parsing.
            if _behavioral_parts:
                system_prompt = system_prompt + "\n\n" + "\n\n".join(_behavioral_parts)
        else:
            # Inject recalled memory context: memories associated with this
            # turn's user prompt, retrieved through the single recall path
            # (associative recall over the layered memory store).
            try:
                memory_prompt = self._ctx_bldr.build_recall_prompt(prompt)
                if memory_prompt:
                    self._ctx_renderer.record("Memory", memory_prompt)
                    system_prompt = system_prompt + "\n\n" + memory_prompt
            except Exception:
                pass

            # Inject relevant profile context -- only fields matching the user's query
            if self.profile_system is not None:
                try:
                    profile_prompt = self._ctx_bldr.build_profile_prompt(prompt)
                    if profile_prompt:
                        self._ctx_renderer.record("Profile", profile_prompt)
                        system_prompt = system_prompt + "\n\n" + profile_prompt
                except Exception:
                    pass

            # Inject agent soul / identity context (SOUL.md, IDENTITY.md, USER.md)
            if self.soul_system is not None:
                try:
                    soul_prompt = self._ctx_bldr.build_soul_prompt()
                    if soul_prompt:
                        self._ctx_renderer.record("Soul", soul_prompt)
                        system_prompt = system_prompt + "\n\n" + soul_prompt
                except Exception:
                    pass

            # Inject meta-cognition self-awareness (known capability
            # weaknesses from the evolution learner).  This was previously
            # only merged into the last *user* message behind the
            # ENCRE_EVOLUTION env gate, which kept it invisible by default.
            # Surfacing it in the system prompt closes the learning loop:
            # the agent sees its own weak domains and can compensate.
            if self.meta is not None:
                try:
                    meta_ctx = self.meta.get_self_awareness_context()
                    if meta_ctx:
                        self._ctx_renderer.record("Self-Awareness", meta_ctx)
                        system_prompt = system_prompt + "\n\n" + meta_ctx
                except Exception:
                    pass

            # Inject device context catalog (L1 鈥?lightweight, always visible)
            try:
                device_prompt = self._ctx_bldr.build_device_context_prompt()
                if device_prompt:
                    self._ctx_renderer.record("Device Context", device_prompt)
                    system_prompt = system_prompt + "\n\n" + device_prompt
            except Exception:
                pass

        stage_prompt = self._build_stage_prompt()
        if stage_prompt:
            self._ctx_renderer.record("Task Stage", stage_prompt)
            system_prompt = system_prompt + "\n\n" + stage_prompt
        working_set_prompt = self._build_working_set_prompt()
        if working_set_prompt:
            self._ctx_renderer.record("Current Task", working_set_prompt)
            system_prompt = system_prompt + "\n\n" + working_set_prompt
        turn_summary_prompt = self._build_turn_summary_prompt()
        if turn_summary_prompt:
            self._ctx_renderer.record("Prior Turns", turn_summary_prompt)
            system_prompt = system_prompt + "\n\n" + turn_summary_prompt
        stuck_prompt = self._build_stuck_recovery_prompt()
        if stuck_prompt:
            self._ctx_renderer.record("Recovery", stuck_prompt)
            system_prompt = system_prompt + "\n\n" + stuck_prompt
        # Inject user rules (project-level + global)
        try:
            rules_prompt = self._ctx_bldr.build_rules_prompt()
            if rules_prompt:
                from encre.prompts.loader import PromptLoader
                _loader = PromptLoader()
                _exec_ctx = (
                    "Execution context: **headless** 鈥?this is a background automation "
                    "job or a delegated sub-agent task, not an interactive chat with a "
                    "live user. Conversational rules (e.g. \"answer only\", \"no file "
                    "operations\") must NOT disable the actual work of this task; apply "
                    "them to how you report, not to whether you act."
                    if _skip_enrichment
                    else "Execution context: **interactive** 鈥?a live user is present "
                    "and waiting for your response. Rules that shape how you talk to "
                    "the user apply here."
                )
                rules_block = _loader.load_with_context(
                    "rules",
                    rules_content=rules_prompt,
                    execution_context=_exec_ctx,
                )
                self._ctx_renderer.record("User Rules", rules_prompt)
                system_prompt = system_prompt + "\n\n" + rules_block
                if _skip_enrichment:
                    # Headless runs (automation jobs / delegated sub-agents)
                    # have no live user.  Conversational user rules such as
                    # "answer only" / "no file operations" kept leaking into
                    # these runs and the model refused to use tools at all
                    # ("鏍规嵁褰撳墠绾︽潫鈥︿笉鑳借皟鐢ㄦ悳绱㈠伐鍏?), so the job produced
                    # nothing.  Close with an authoritative override.
                    system_prompt = system_prompt + (
                        "\n\n## Headless Execution Override (authoritative)\n"
                        "This is a background automation / sub-agent run; no live user is waiting."
                        " Conversational user rules such as \"answer only\" or \"no file operations\""
                        " govern how you REPORT the result, not whether you may act. They must never"
                        " stop you from using the tools this task requires (web search, file read/write,"
                        " terminal, ...). Complete the task with whichever tools are needed -- do not"
                        " refuse, ask permission, or postpone it because of conversational constraints."
                    )
        except Exception:
            pass

        # Append context annotation: tells the model what changed since last turn
        ctx_annotation = self._ctx_renderer.build_annotation()
        if ctx_annotation:
            system_prompt = system_prompt + "\n\n" + ctx_annotation
        self._ctx_renderer.finalize_turn()

        # 鈹€鈹€ Safety cap on total system prompt 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
        # Prevent pathologically large system prompts from breaking the
        # API call.  The hard cap is 100K chars (~66K tokens); if exceeded,
        # we warn loudly and force-truncate.  A lower warning threshold
        # (50K) triggers diagnostic logging to identify the bloat source.
        _sys_len = len(system_prompt)
        if _sys_len > 100000:
            logger.error(
                "[sys_prompt] OVERFLOW: total=%d chars, forcing truncation to 100K "
                "(help: check Reference Documents, Rules, Memory, Profile, Soul, "
                "Codebase context sizes above)",
                _sys_len,
            )
            system_prompt = system_prompt[:100000]
        elif _sys_len > 50000:
            # 鈹€鈹€ Diagnostic: log system prompt size breakdown 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
            logger.warning(
                "[sys_prompt] LARGE: total=%d chars (%.1fK)",
                _sys_len, _sys_len / 1024,
            )
            _parts: list[tuple[str, str]] = []
            for _varname in (
                "codebase_ctx", "skill_prompt", "tool_skills_prompt",
                "doc_skills_prompt", "memory_prompt", "profile_prompt",
                "soul_prompt", "stage_prompt",
                "working_set_prompt", "turn_summary_prompt", "stuck_prompt",
                "rules_prompt", "ctx_annotation",
            ):
                _val = locals().get(_varname, "") or ""
                if len(str(_val)) > 1000:
                    _parts.append((_varname, str(_val)))
            # Common enrichment sections that always exist
            _req = _req_summary if not _skip_enrichment else ""
            if len(_req or "") > 500:
                _parts.append(("req_summary", _req or ""))
            if custom_instructions:
                _parts.append(("custom_instructions", custom_instructions))
            # Log each contributor
            for _name, _val in sorted(_parts, key=lambda x: -len(x[1])):
                _n = len(_val)
                logger.warning(
                    "[sys_prompt]   %-24s %d chars (%.1fK)", _name, _n, _n / 1024,
                )
            # Base blocks estimate
            _enrich_total = sum(len(v) for _, v in _parts)
            _base_est = _sys_len - _enrich_total
            logger.warning(
                "[sys_prompt]   %-24s ~%d chars (base blocks + fixed blocks)",
                "base_blocks (est)", max(0, _base_est),
            )

        # Update system message on every run so prompt blocks match current intents
        has_system = any(
            m.get("role") == "system" and m.get("branch_id", self.session.active_branch_id) == self.session.active_branch_id
            for m in self.session.messages
        )
        if has_system:
            for i, m in enumerate(self.session.messages):
                if m.get("role") == "system" and m.get("branch_id", self.session.active_branch_id) == self.session.active_branch_id:
                    self.session.messages[i] = {"role": "system", "content": system_prompt, "branch_id": self.session.active_branch_id}
                    self.session.mark_messages_dirty()
                    break
        else:
            self.session.messages.insert(0, {"role": "system", "content": system_prompt, "branch_id": self.session.active_branch_id})
            self.session.mark_messages_dirty()

        # Add user prompt if not a duplicate of the last user message
        # in the active branch context (not just self.session.messages[-1],
        # which may be from a different branch during retry).
        ctx_msgs = self.session.get_context_messages()
        last_ctx_user = None
        for m in reversed(ctx_msgs):
            if m.get("role") == "user":
                last_ctx_user = m
                break
        if last_ctx_user is None or last_ctx_user.get("content") != prompt:
            if skill_prompt and last_ctx_user is not None:
                # Skill was activated -- don't add a duplicate with the stripped text.
                # Keep the original message content so the user sees what they typed.
                pass
            else:
                logger.info("[sub_agent] adding user message to session | prompt_len=%s | last_ctx_user_exists=%s",
                            len(prompt), last_ctx_user is not None)
                self.session.add_message("user", prompt)
                # Flush the user message to disk before entering the model
                # loop so a process kill here still leaves a resumable
                # transcript.  Mirrors Claude Code QueryEngine.ts:450-463.
                with contextlib.suppress(Exception):
                    await self.hook_system.emit_user_message_persisted(
                        self.session.id or ""
                    )
                self.event_stream.publish(UserMessagePersisted(
                    session_id=self.session.id or "",
                ))

        if time.time() - _t0 > 0.1:
            logger.info("[perf] prompt build %.1fs", time.time() - _t0)

        t.system_prompt = system_prompt
        # Re-snapshot AFTER the user prompt has been persisted above: the
        # earlier ``ctx_msgs`` snapshot was taken before the dedup check, so
        # assigning it directly would send the first model request of the run
        # to the backend without the user's message.  For sub-agents (which
        # pre-add the prompt) this re-fetch returns the identical cached list.
        t.context_msgs = self.session.get_context_messages()


# ``build_delegation_guidance`` is imported lazily at call time in the
# original monolith module scope; import it here for the enrichment block.
from encre.loop_stability import build_delegation_guidance  # noqa: E402


class SystemPromptPhase(LoopPhase):
    """Run-scoped phase: classify intent and build the system prompt.

    Wraps :meth:`PhasePromptMixin._build_turn_system_prompt` -- the exact
    code the runner used to call inline before the turn loop.
    """

    name = "system_prompt"
    scope = PhaseScope.RUN

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        await self._loop._build_turn_system_prompt(ctx.turn)
        return PhaseResult.continue_()


class SessionStartPhase(LoopPhase):
    """Run-scoped phase: session-start lifecycle and context sanitisation.

    Wraps the block the runner used to execute directly after the prompt
    phase: the ``session_start`` hook + event, the standing-orders /
    checkpoint-gate initialisation, and the one-time branch sanitisation of
    a freshly loaded session.
    """

    name = "session_start"
    scope = PhaseScope.RUN

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        loop = self._loop
        t = ctx.turn
        _t_hook = time.time()
        await loop.hook_system.emit_session_start()
        loop.event_stream.publish(SessionStarted(session_id=loop.session.id or ""))
        logger.info("[run] emit_session_start done (%.2fs)", time.time() - _t_hook)

        # Standing-orders reminder: loaded once per ``run()`` and merged into
        # the LAST user message before the first model call of a fresh user
        # turn (main agent only).  Re-arms the binding pre-action constraints
        # (recall-first, clarify, checkpoint) right where the model begins
        # responding; the full gates still live in the system prompt.
        t.standing_orders_text = (
            build_standing_orders_reminder() if not t.skip_enrichment else ""
        )
        t.standing_orders_injected = False

        # Checkpoint hard-gate state: injected once per run() when the number
        # of consecutive tool-call steps (since the last user message) crosses
        # the threshold AND the user has not explicitly authorised a hands-off
        # run.  The injected message hands control back to the user -- the
        # code-level mirror of Gate 4 in mandatory_constraints.prompt.
        t.checkpoint_injected_this_run = False
        t.checkpoint_text = ""

        # Sanitize session messages on every run -- old sessions loaded from
        # disk may contain broken tool_call groups (from crashes) that cause
        # 400 errors.  Only sanitize active branch context; other branches
        # remain untouched.
        active_branch_id = loop.session.active_branch_id
        if active_branch_id not in loop._sanitized_branches:
            loop.session.replace_branch_messages(
                active_branch_id, loop.compact_engine.sanitize(t.context_msgs),
            )
            loop._sanitized_branches.add(active_branch_id)
            t.context_msgs = loop.session.get_context_messages()
        t.active_branch_id = active_branch_id
        return PhaseResult.continue_()
