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

"""Prompt phase: build (and cache) the per-run system prompt.

Extracted verbatim from the original monolithic ``_run_impl`` preparation
section.  ``_build_turn_system_prompt`` classifies intent, activates
``/skill`` invocations, assembles every enrichment block (workspace,
codebase index, architecture contract, Plan-Do-Review, memory, profile,
soul, meta-cognition, device context, stage / working-set / turn-summary /
stuck-recovery, user rules, context annotation) and updates the session's
system + user messages.
"""

import asyncio
import contextlib
import hashlib
import time
from datetime import datetime as _dt
from pathlib import Path
from typing import Any

from encre.context_compiler import ContextInputs, ContextSection, compile_context, compute_context_usage
from encre.events.lifecycle import SessionStarted, UserMessagePersisted
from encre.features import adapters as _features
from encre.logging_config import get_logger
from encre.loop.phase_api import (
    LoopPhase,
    PhaseContext,
    PhaseResult,
    PhaseScope,
)
from encre.loop.turn_ctx import TurnContext
from encre.loop_stability import _detect_requirement_change, build_live_register_text, build_standing_orders_reminder
from encre.memory_index import RetrievedFragment, SessionMemoryIndex
from encre.prompts.classifier import classify_intents, classify_register
from encre.prompts.system import skills_block, specialty_block_text
from encre.utils.tokens import count_message_tokens, estimate_tokens

logger = get_logger(__name__)

_shared_memory_index: SessionMemoryIndex | None = None


def _shared_session_memory_index() -> SessionMemoryIndex:
    """Return the process-wide transcript index.

    The index covers the shared ``data/sessions`` tree, so every agent must
    see the same instance -- a per-agent index would rescan the whole tree
    before the first turn of every new session.
    """
    global _shared_memory_index
    if _shared_memory_index is None:
        from encre.paths import get_data_dir

        # Anchor at the real data root: a bare ``Path("data")`` only resolves
        # when the process CWD happens to be the repo root.
        _shared_memory_index = SessionMemoryIndex(get_data_dir())
    return _shared_memory_index


def _tool_fingerprint(payload: list[dict] | None) -> tuple:
    """(name, description) pairs of the tool payload sent to the model.

    The baked tool catalog renders both fields, so the prefix key must
    fingerprint both: a plugin re-activated with a changed description would
    otherwise leave the model reading a catalog that no longer matches its
    payload.
    """
    out: list[tuple[str, str]] = []
    for item in payload or []:
        fn = item.get("function", {}) if isinstance(item, dict) else {}
        out.append((str(fn.get("name", "")), str(fn.get("description", ""))))
    return tuple(out)


def _tool_names(payload: list[dict] | None) -> list[str]:
    """Tool names reachable this turn, in payload order.

    Accepts both provider shapes: OpenAI-style ``{"function": {"name": ...}}``
    and the flat ``{"name": ...}`` used by Anthropic-style payloads.
    """
    names: list[str] = []
    for item in payload or []:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        name = fn.get("name") if isinstance(fn, dict) else None
        if not name:
            name = item.get("name")
        if not name:
            continue
        name = str(name).strip()
        if not name or name in names:
            continue
        names.append(name)
    return names


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
        # Loop-boundary reasoning finalization: providers that strip
        # interleaved reasoning from history (e.g. DeepSeek) finalize closed
        # tool loops ONCE here instead of re-sanitizing a growing suffix on
        # every request, which kept mutating the request prefix and busting
        # the provider prompt cache.  This runs once per run() (never inside
        # an in-progress tool loop), so current-loop reasoning is preserved.
        _finalize_history = getattr(self.backend, "finalize_history", None)
        if callable(_finalize_history):
            try:
                # finalize runs on the EFFECTIVE context (view + tail) and is
                # installed as a new view overlay -- the persisted history in
                # the turn files is never rewritten.
                _ctx_now = self.session.get_context_messages()
                # Pass the session so DeepSeek's finalize can HEAL reasoning
                # that a stale pre-fix view (persisted in view.json) had
                # stripped from tool-call assistants -- the append-only
                # branch still holds the original.
                _finalized = _finalize_history(_ctx_now, self.session)
                if _finalized is not None:
                    _upto = max(
                        (m.get("seq_in_branch", -1) for m in _ctx_now),
                        default=-1,
                    )
                    self.session.apply_compacted_view(
                        self.session.active_branch_id,
                        _finalized,
                        _upto,
                    )
            except Exception:
                logger.debug("[run] finalize_history failed", exc_info=True)

        # Attach the append-only trajectory recorder once per session: every
        # model-visible event (messages, injections, compactions) lands in
        # data/sessions/<id>/events.jsonl for resume/fork/replay/audit.
        if getattr(self.session, "trajectory_sink", None) is None:
            try:
                from encre.loop.trajectory import TrajectoryRecorder

                _recorder = TrajectoryRecorder(Path("data", "sessions", self.session.id or "unknown"))
                self.session.trajectory_sink = _recorder.on_session_event
                _recorder.record("session.attached", loop=type(self).__name__)
            except Exception:
                logger.debug("[trajectory] recorder attach failed", exc_info=True)

        # Classify user intent for dynamic prompt assembly, and resolve the
        # response register (chat / answer / work) that decides WHICH
        # behavioural frames are in force at all.  The two axes are
        # independent: intent is the domain, register is the shape.  A
        # workspace_root is needed for the ambiguous default, so the workspace
        # info is hoisted here and reused by the builder call below.
        intents = classify_intents(t.prompt)
        t.intents = intents
        ws_root, ws_name, ws_summary = self._ctx_bldr.workspace_info()
        register = classify_register(t.prompt, workspace_root=ws_root)
        # A delegated or headless run is `work` by definition: there is no one
        # on the other side to chat with, and its instruction text is a work
        # order.  Without this guard a terse automation prompt ("summarise the
        # logs") could classify as conversation and switch the delivery blocks
        # off for a job that has nobody to answer a clarifying question.
        if self.sub_agent_depth > 0 or t.system_prompt is not None or self._profile.mode == "automation":
            register = "work"
        t.register = register

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
            and (not self._pdr_active or _detected_change)
        ):
            # Rich decomposition first: one bounded thinking-off model call
            # turns unstructured prose into real steps.  Fail-closed to the
            # language-agnostic structural parsers when the model is
            # unavailable or its answer is unparsable -- the plan degrades,
            # it is never fabricated.
            _model_steps: list[dict[str, str]] | None = None
            try:
                _model_steps = await _features.decompose_task_with_model(
                    t.prompt, self.backend, max_steps=self._pdr.MAX_STEPS,
                )
            except Exception:
                logger.warning("[run] PDR model decomposition crashed; structural fallback", exc_info=True)
                _model_steps = None
            if _model_steps:
                self._pdr.initialize(t.prompt, steps=_model_steps)
            else:
                self._pdr.initialize(t.prompt)
            self._pdr_active = True
            self._pdr.start_next_step()
            logger.info(
                "[run] Plan-Do-Review initialized for task (steps=%d, source=%s)",
                len(self._pdr.plan.steps),
                "model" if _model_steps else "structural",
            )

        # Activate any skills invoked via /skill-name syntax
        skill_prompt, prompt = await self._activate_skills(t.prompt)
        t.skill_prompt = skill_prompt
        t.prompt = prompt
        system_prompt = t.system_prompt
        custom_instructions = t.custom_instructions
        slash_command_mode = t.slash_command_mode
        slash_commands = t.slash_commands
        _t0 = time.time()
        # 当前模型的行为画像: 驱动工具可见性裁剪与 system prompt 减负。
        _model_profile = self._model_profile()
        tools = None
        if self.backend.supports_tool_calling():
            # Discovery handles ToolSet resolution + manage(discover) + MCP merging.
            # Expand the base toolset with intent-matched sets (coding /
            # research / data) so advanced tools are reachable without the
            # model having to self-select discovery first.  The resolved
            # name is bookkeeping only: registration IS activation, so the
            # model-facing payload contains every registered tool -- unless
            # a light-tier ModelProfile narrows visibility for a weak model
            # (execution stays unrestricted either way).
            self._tool_set_name = self._resolve_tool_set_for_mode(intents)
            tools = self.discovery.get_active_tools_payload(self.session.id, fmt="openai", profile=_model_profile)

        # Capture whether a custom system prompt was provided BEFORE it is
        # built below.  Once built/prepended, ``system_prompt`` is never
        # None again, so this must be recorded up front -- checking it
        # afterwards always reads False and silently degrades enrichment.
        _original_system_prompt_was_none = system_prompt is None
        _skip_enrichment = not _original_system_prompt_was_none and slash_command_mode not in ("plan", "spec")

        # The fingerprint of everything the built system prompt depends on.  ONE
        # tuple serves both decisions -- whether the builder has to run again,
        # and whether the frozen system message has to be rebuilt.  They used to
        # be two nearly-identical tuples that drifted: the builder cache noticed
        # a changed response language, workspace root, slash-command list or
        # tool description, rebuilt the prompt, and the freeze check then
        # discarded the fresh result, so the session ran on a prefix describing
        # state it no longer had.
        #
        # Deliberately absent: the project snapshot, the skill catalogue and the
        # domain specialty -- all three change inside a session and are supplied
        # as content-hashed context blocks instead, so refreshing them never
        # rewrites (or re-bills) the cached prefix.  The response register is
        # absent for the same reason: every register section stays in the prefix
        # and the live one is declared per turn.
        _prefix_key = (
            self.session.id,
            self.config.permission_mode,
            slash_command_mode,
            self.config.model,
            (self.config.active_command or {}).get("prompt", ""),
            self.active_command_name,
            custom_instructions,
            bool(_skip_enrichment),
            _tool_fingerprint(tools),
            _model_profile.cache_key,
            self.config.language_preference,
            self.config.language,
            ws_root,
            ws_name,
            tuple(c.get("name", "") for c in (slash_commands or [])),
            # Re-freeze once per calendar day: the date is baked into the prefix.
            _dt.now().date().isoformat(),
        )

        if system_prompt is None:
            # Cache the base system prompt by this key so we don't rebuild it
            # every turn when nothing changed.
            if self._sys_prompt_cache is not None and self._sys_prompt_cache_key == _prefix_key:
                system_prompt = self._sys_prompt_cache
            else:
                system_prompt = self.prompt_builder.build_system_prompt(
                    self.config.permission_mode,
                    tools=tools,
                    intents=intents,
                    workspace_root=ws_root,
                    workspace_name=ws_name,
                    project_summary="",
                    language_preference=self.config.language_preference,
                    app_language=self.config.language,
                    custom_instructions=custom_instructions,
                    session_id=self.session.id,
                    slash_command_mode=slash_command_mode,
                    slash_commands=slash_commands,
                    skill_summary="",
                    active_command=getattr(self.config, "active_command", None),
                    model=self.config.model,
                    model_profile=_model_profile,
                    include_specialty=False,
                )
                self._sys_prompt_cache = system_prompt
                self._sys_prompt_cache_key = _prefix_key
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
                project_summary="",
                language_preference=self.config.language_preference,
                app_language=self.config.language,
                custom_instructions=custom_instructions,
                session_id=self.session.id,
                slash_command_mode=slash_command_mode,
                slash_commands=slash_commands,
                skill_summary="",
                active_command=getattr(self.config, "active_command", None),
                model=self.config.model,
                model_profile=_model_profile,
                include_specialty=False,
            )
            system_prompt = system_prompt + "\n\n" + built
        else:
            # Custom system_prompt provided but we are in normal mode.  The
            # custom prompt may be silent about the current mode, so the model
            # can infer it incorrectly from earlier plan/spec messages.  Force
            # an explicit normal-mode declaration at the top.
            system_prompt = (
                "Current mode: NORMAL MODE. You are not in plan mode or spec "
                "mode. Ignore any mode claims in earlier messages; this "
                "system instruction is authoritative.\n\n" + system_prompt
            )

        # ``_skip_enrichment`` was already derived above (it participates in the
        # prefix key); this only publishes it to the turn context.
        t.skip_enrichment = _skip_enrichment

        # ── Cache-stable prompt assembly (OpenCode v2 pattern) ──
        # The system message is FROZEN for the whole session: it is written
        # once and only rebuilt when discrete state changes -- the exact set of
        # inputs is what ``_prefix_key`` fingerprints.  Per-turn varying blocks
        # (codebase index, PDR progress, memory recall, project snapshot, skill
        # catalogue, domain specialty, live register) are appended to HISTORY as
        # synthetic "[CONTEXT UPDATE]" messages -- and only when their content
        # actually changed.  Rewriting the system message on every run mutated
        # the request prefix and busted the provider prompt cache on every turn.
        if getattr(self, "_ctx_block_hashes", None) is None:
            self._ctx_block_hashes: dict[str, str] = {}
        # Monotonic "real work started" signal: True once ANY assistant
        # message in this session carried tool calls.  Purely mechanical
        # transcript state -- NO intent/prompt-text classification.  Work
        # blocks (codebase index, delegation guidance) are only supplied
        # once this flag is set, so a pure-chat session ("hi") freezes a
        # minimal system prompt.  When the flag flips later, the blocks
        # begin to be supplied and reach the model as a [CONTEXT UPDATE]
        # delta -- the hash-delta machinery below handles re-injection.
        if getattr(self, "_saw_tool_activity", False) is not True:
            self._saw_tool_activity = self._session_has_tool_activity()
        _tool_activity = bool(self._saw_tool_activity)
        # The freeze decision uses the SAME fingerprint the builder cache uses:
        # if the built prefix would differ, the frozen one must be replaced.
        # Keeping these as two separate tuples is what previously let a changed
        # response language, workspace root, slash-command list or tool
        # description get rebuilt and then silently discarded.
        _is_frozen_turn = getattr(self, "_frozen_system_prompt", None) is not None and getattr(self, "_frozen_sys_sig", None) == _prefix_key
        # ── Compiler-driven block supply (priority table lives in
        # encre.context_compiler) ──  Producers below only *supply* candidate
        # blocks into sections; the context compiler alone decides section
        # ordering, token budgets and truncation (see the compile step at the
        # end of this method).
        _sections: dict[ContextSection, list[tuple[str, str]]] = {}
        _recall_fragments: list[RetrievedFragment] = []

        def _supply(section: ContextSection, name: str, content: str) -> None:
            if content:
                _sections.setdefault(section, []).append((name, content))

        # Per-mode behaviour preamble (GENERAL / WORKSPACE / AUTOMATION) so
        # the model actually feels the mode difference, not just the
        # internal tuning knobs.  Recorded under "Mode" so the context
        # annotation tracks it across turns.
        _mode_gain = getattr(self._profile, "prompt_gain", "") or ""
        if not _skip_enrichment and _mode_gain:
            self._ctx_renderer.record("Mode", _mode_gain)
            _supply(ContextSection.BACKGROUND, "Mode", _mode_gain)

        # Live register declaration.  The frozen prefix carries the sections of
        # all three registers so it never has to be rebuilt when the turn's
        # shape changes (a rebuild would void the provider prompt cache); this
        # block says which one governs now and lists the inert ones.  The
        # context compiler hashes it, so it reaches the model once and again
        # only on a real transition.
        if not _skip_enrichment:
            _register_text = build_live_register_text(register, system_prompt)
            if _register_text:
                self._ctx_renderer.record("Live Register", _register_text)
                _supply(ContextSection.BACKGROUND, "Live Register", _register_text)

        # Domain specialty (coding / research / data / general): the guidance
        # that matches what the user is asking ABOUT this turn.  A context block
        # rather than a prefix section, so a topic change costs one small
        # appended delta instead of a full prefix rebuild -- and so a session
        # that opened with "hi" is not locked into general guidance once the
        # user asks for code.  Supplied for delegated runs too: a sub-agent used
        # to get this section baked into its prefix, and dropping the bake must
        # not silently drop the guidance.
        _specialty_text = specialty_block_text(getattr(self.prompt_builder, "specialty", "general"), intents)
        if _specialty_text:
            self._ctx_renderer.record("Specialty", _specialty_text)
            _supply(ContextSection.BACKGROUND, "Specialty", _specialty_text)

        # Tool usage guides.  Guides for individual tools are excluded from the
        # skill catalogue (one per tool, long bodies), so the model would never
        # learn they exist -- and the full text used to arrive only *after* a
        # tool had already run.  A compact index keyed to the tools actually
        # exposed this turn lets the model activate the right guide up front;
        # loaded guides drop out of the index and are expanded in full below.
        if tools:
            _tool_index_text = self._render_tool_skill_index(_tool_names(tools))
            if _tool_index_text:
                self._ctx_renderer.record("Tool Skill Index", _tool_index_text)
                _supply(ContextSection.BACKGROUND, "Tool Skill Index", _tool_index_text)

        # Skill catalogue.  Same reasoning as the project snapshot: installed
        # and removed skills change during a session, and the catalogue must
        # track them without rewriting the cached prefix.
        _catalogue = skills_block(self._render_skill_catalogue())
        if _catalogue is not None:
            self._ctx_renderer.record("Skills", _catalogue.content)
            _supply(ContextSection.BACKGROUND, "Skills", _catalogue.content)

        # Project snapshot.  Deliberately NOT baked into the frozen prefix: the
        # snapshot regenerates as the project changes, and embedding it would
        # either freeze a stale picture in the cached prefix or force a rebuild
        # every time it refreshed.  As a delta it is both current and cheap.
        if not _skip_enrichment and ws_summary:
            self._ctx_renderer.record("Project Snapshot", ws_summary)
            _supply(ContextSection.BACKGROUND, "Project Snapshot", ws_summary)

        # Workspace directory tree.  Supplied only once real tool activity
        # exists in the session: a pure-chat turn must not bake a large tree
        # into the frozen system prompt.  When work starts later it arrives as
        # a [CONTEXT UPDATE] delta instead.
        if not _skip_enrichment and _tool_activity and ws_root:
            tree_ctx = self._ctx_bldr.build_directory_tree(ws_root)
            if tree_ctx:
                self._ctx_renderer.record("Workspace Structure", tree_ctx)
                _supply(ContextSection.BACKGROUND, "Workspace Structure", tree_ctx)

        # Architecture contract artifact (workspace mode): design decisions
        # agreed by the planner/architect role are BINDING for
        # implementation.  Re-surfaced whenever it changes so the coder
        # stays anchored to the agreed architecture.
        if not _skip_enrichment and self._profile.contract_inject:
            contract_ctx = self._ctx_bldr.build_architecture_contract()
            if contract_ctx:
                self._ctx_renderer.record("Architecture Contract", contract_ctx)
                _supply(ContextSection.BACKGROUND, "Architecture Contract", contract_ctx)

        # Plan-Do-Review progress (WORKSPACE profile): the current step,
        # success criteria, and progress summary keep the coder anchored to
        # the written plan across turns (mirrors Claude Code's
        # plan-do-review).  Emitted as a delta whenever progress advances.
        if not _skip_enrichment and self._pdr_active:
            pdr_ctx = self._pdr.get_context()
            if pdr_ctx:
                self._ctx_renderer.record("Plan-Do-Review", pdr_ctx)
                _supply(ContextSection.BACKGROUND, "Plan-Do-Review", pdr_ctx)

        # Skill / tool-skill / doc-skill prompts (doc -> tool -> skill order
        # matches the previous prepend order so downstream content reads the
        # same).  Emitted as a delta whenever the active skill set changes.
        doc_skills_prompt = self._render_active_doc_skills()
        if doc_skills_prompt:
            _supply(ContextSection.BACKGROUND, "Doc Skills", doc_skills_prompt)

        tool_skills_prompt = self._render_active_tool_skills()
        if tool_skills_prompt:
            _supply(ContextSection.BACKGROUND, "Tool Skills", tool_skills_prompt)

        if skill_prompt:
            _supply(ContextSection.BACKGROUND, "Skill Prompt", skill_prompt)

        # User requirements summary: a compact description of the user's
        # core goals extracted from the last compact summary.  Lives in
        # session metadata so it survives compaction.  Only for the main
        # agent (sub-agents get their own self-contained brief).
        if not _skip_enrichment:
            _req_summary = self._state_mgr.user_requirements_summary
            if _req_summary:
                _supply(ContextSection.BACKGROUND, "User Requirements", _req_summary)
            # P5: coordinator-style delegation guidance.  Steers the main
            # agent toward good delegation hygiene (understand -> delegate
            # self-contained briefs -> synthesise) on complex multi-step
            # work.  Mirrors Claude Code's coordinatorMode.ts.  (Static
            # content -- hashed to zero deltas on every turn after the
            # freeze turn.)  Supplied only once real tool activity exists:
            # delegation guidance is meaningless in a pure-chat session and
            # would only bloat the frozen prefix.
            if _tool_activity:
                _supply(ContextSection.BACKGROUND, "Delegation", build_delegation_guidance())

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
                    logger.debug("exception suppressed", exc_info=True)
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
                _supply(ContextSection.SAFETY_RULES, "Behavioral", "\n\n".join(_behavioral_parts))
        else:
            # Inject recalled memory context: memories associated with this
            # turn's user prompt, retrieved through the single recall path
            # (associative recall over the layered memory store).
            try:
                memory_prompt = self._ctx_bldr.build_recall_prompt(prompt)
                if memory_prompt:
                    self._ctx_renderer.record("Memory", memory_prompt)
                    _supply(ContextSection.RETRIEVED_MEMORY, "Memory", memory_prompt)
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
            # BM25 session recall: lazily build/refresh the on-disk memory
            # index and retrieve past sessions relevant to this prompt (the
            # current session is excluded).  Failure degrades to no
            # retrieval -- never blocks the main flow.
            try:
                _recall_fragments = await self._collect_session_recall_fragments(prompt)
                if _recall_fragments:
                    logger.info("[run] session recall hits: %d", len(_recall_fragments))
            except Exception:
                _recall_fragments = []
                logger.debug("[run] session recall failed", exc_info=True)

            # Inject relevant profile context -- only fields matching the user's query
            if self.profile_system is not None:
                try:
                    profile_prompt = self._ctx_bldr.build_profile_prompt(prompt)
                    if profile_prompt:
                        self._ctx_renderer.record("Profile", profile_prompt)
                        _supply(ContextSection.BACKGROUND, "Profile", profile_prompt)
                except Exception:
                    logger.debug("exception suppressed", exc_info=True)
            # Inject agent soul / identity context (SOUL.md, IDENTITY.md, USER.md)
            if self.soul_system is not None:
                try:
                    soul_prompt = self._ctx_bldr.build_soul_prompt()
                    if soul_prompt:
                        self._ctx_renderer.record("Soul", soul_prompt)
                        _supply(ContextSection.IDENTITY, "Soul", soul_prompt)
                except Exception:
                    logger.debug("exception suppressed", exc_info=True)
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
                        _supply(ContextSection.BACKGROUND, "Self-Awareness", meta_ctx)
                except Exception:
                    logger.debug("exception suppressed", exc_info=True)
            # Inject device context catalog (L1 — lightweight, always visible)
            try:
                device_prompt = self._ctx_bldr.build_device_context_prompt()
                if device_prompt:
                    self._ctx_renderer.record("Device Context", device_prompt)
                    _supply(ContextSection.BACKGROUND, "Device Context", device_prompt)
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
        stage_prompt = self._build_stage_prompt()
        if stage_prompt:
            self._ctx_renderer.record("Task Stage", stage_prompt)
            _supply(ContextSection.BACKGROUND, "Task Stage", stage_prompt)
        working_set_prompt = self._build_working_set_prompt()
        if working_set_prompt:
            self._ctx_renderer.record("Current Task", working_set_prompt)
            _supply(ContextSection.BACKGROUND, "Current Task", working_set_prompt)
        turn_summary_prompt = self._build_turn_summary_prompt()
        if turn_summary_prompt:
            self._ctx_renderer.record("Prior Turns", turn_summary_prompt)
            _supply(ContextSection.BACKGROUND, "Prior Turns", turn_summary_prompt)
        stuck_prompt = self._build_stuck_recovery_prompt()
        if stuck_prompt:
            self._ctx_renderer.record("Recovery", stuck_prompt)
            _supply(ContextSection.BACKGROUND, "Recovery", stuck_prompt)
        # Inject user rules (project-level + global)
        try:
            rules_prompt = self._ctx_bldr.build_rules_prompt()
            if rules_prompt:
                from encre.prompts.loader import PromptLoader

                _loader = PromptLoader()
                _exec_ctx = (
                    "Execution context: **headless** — this is a background automation "
                    "job or a delegated sub-agent task, not an interactive chat with a "
                    'live user. Conversational rules (e.g. "answer only", "no file '
                    'operations") must NOT disable the actual work of this task; apply '
                    "them to how you report, not to whether you act."
                    if _skip_enrichment
                    else "Execution context: **interactive** — a live user is present "
                    "and waiting for your response. Rules that shape how you talk to "
                    "the user apply here."
                )
                rules_block = _loader.load_with_context(
                    "rules",
                    rules_content=rules_prompt,
                    execution_context=_exec_ctx,
                )
                self._ctx_renderer.record("User Rules", rules_prompt)
                _supply(ContextSection.SAFETY_RULES, "User Rules", rules_block)
                if _skip_enrichment:
                    # Headless runs (automation jobs / delegated sub-agents)
                    # have no live user.  Conversational user rules such as
                    # "answer only" / "no file operations" kept leaking into
                    # these runs and the model refused to use tools at all
                    # ("根据当前约束…不能调用搜索工具"), so the job produced
                    # nothing.  Close with an authoritative override.
                    _supply(
                        ContextSection.SAFETY_RULES,
                        "Headless Override",
                        (
                            "\n\n## Headless Execution Override (authoritative)\n"
                            "This is a background automation / sub-agent run; no live user is waiting."
                            ' Conversational user rules such as "answer only" or "no file operations"'
                            " govern how you REPORT the result, not whether you may act. They must never"
                            " stop you from using the tools this task requires (web search, file read/write,"
                            " terminal, ...). Complete the task with whichever tools are needed -- do not"
                            " refuse, ask permission, or postpone it because of conversational constraints."
                        ),
                    )
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        # ── Compile: single budget / order authority ──
        # The context compiler alone owns section ordering, token budgets
        # and truncation (priority table: identity > safety_rules >
        # steering > recent_history > retrieved_memory > tool_results >
        # background).  Recent history stays owned by the compact pipeline
        # (compaction already ran before this phase), and steering
        # transport is handled at drain boundaries via
        # ``compile_steering_messages``, so both sections are empty here.
        _ctx_window = 0
        try:
            _ctx_window = int(self.backend.context_window_size() or 0)
        except Exception:
            _ctx_window = 0
        _used_tokens = 0
        try:
            _used_tokens = count_message_tokens(self.session.get_context_messages()) + estimate_tokens(system_prompt)
        except Exception:
            _used_tokens = 0
        _compiled = compile_context(
            ContextInputs(
                soul_files={
                    "soul": "\n\n".join(c for _n, c in _sections.get(ContextSection.IDENTITY, []) if c),
                },
                safety_rules="\n\n".join(c for _n, c in _sections.get(ContextSection.SAFETY_RULES, []) if c),
                retrieved_fragments=[] if _skip_enrichment else _recall_fragments,
                retrieved_blocks=_sections.get(ContextSection.RETRIEVED_MEMORY, []),
                background_blocks=_sections.get(ContextSection.BACKGROUND, []),
                context_window=_ctx_window,
                used_tokens=_used_tokens,
                model=self.config.model,
            )
        )
        _dyn = _compiled.blocks
        with contextlib.suppress(Exception):
            self.telemetry.record_context_breakdown({section.name.lower(): tokens for section, tokens in _compiled.sections_tokens.items()})

        # Context annotation: what changed since the last turn.  On the
        # freeze turn it closes the system prompt; on frozen turns it opens
        # the synthetic context-update message instead (see below).
        ctx_annotation = self._ctx_renderer.build_annotation()
        self._ctx_renderer.finalize_turn()

        if not _is_frozen_turn:
            # ── Freeze turn: bake base + all current blocks into the system
            # prompt, then snapshot block hashes for delta detection.
            system_prompt = system_prompt + "".join("\n\n" + content for _block_name, content in _dyn)
            if ctx_annotation:
                system_prompt = system_prompt + "\n\n" + ctx_annotation
            self._frozen_system_prompt = system_prompt
            self._frozen_sys_sig = _prefix_key
            for _block_name, _block_content in _dyn:
                self._ctx_block_hashes[_block_name] = hashlib.sha256(_block_content.encode("utf-8")).hexdigest()
        else:
            # ── Frozen turn: the system prompt stays byte-identical.  Emit
            # only the blocks whose content changed since the last turn as
            # one synthetic history entry (OpenCode v2 "instruction
            # update"), so the provider prompt cache prefix survives.
            system_prompt = self._frozen_system_prompt or system_prompt
            _changed_blocks: list[tuple[str, str]] = []
            for _block_name, _block_content in _dyn:
                _block_hash = hashlib.sha256(_block_content.encode("utf-8")).hexdigest()
                if self._ctx_block_hashes.get(_block_name) != _block_hash:
                    self._ctx_block_hashes[_block_name] = _block_hash
                    _changed_blocks.append((_block_name, _block_content))
            if _changed_blocks or ctx_annotation:
                _update_parts = [f"## {name}\n{content}" for name, content in _changed_blocks]
                if ctx_annotation:
                    _update_parts.append(ctx_annotation)
                _update = "[CONTEXT UPDATE]\n\n" + "\n\n".join(_update_parts)
                if len(_update) > 60000:
                    _update = _update[:60000] + "\n\n[CONTEXT UPDATE TRUNCATED]"
                self.session.add_message("user", _update, is_synthetic=True)
                _traj_sink = getattr(self.session, "trajectory_sink", None)
                if callable(_traj_sink):
                    with contextlib.suppress(Exception):
                        _traj_sink(
                            "context.injected",
                            {
                                "blocks": [name for name, _ in _changed_blocks],
                            },
                        )

        # ── Safety cap on total system prompt ─────────────────────────
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
            # ── Diagnostic: log system prompt size breakdown ──────────
            logger.warning(
                "[sys_prompt] LARGE: total=%d chars (%.1fK)",
                _sys_len,
                _sys_len / 1024,
            )
            _parts: list[tuple[str, str]] = []
            for _varname in (
                "codebase_ctx",
                "skill_prompt",
                "tool_skills_prompt",
                "doc_skills_prompt",
                "memory_prompt",
                "profile_prompt",
                "soul_prompt",
                "stage_prompt",
                "working_set_prompt",
                "turn_summary_prompt",
                "stuck_prompt",
                "rules_prompt",
                "ctx_annotation",
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
                    "[sys_prompt]   %-24s %d chars (%.1fK)",
                    _name,
                    _n,
                    _n / 1024,
                )
            # Base blocks estimate
            _enrich_total = sum(len(v) for _, v in _parts)
            _base_est = _sys_len - _enrich_total
            logger.warning(
                "[sys_prompt]   %-24s ~%d chars (base blocks + fixed blocks)",
                "base_blocks (est)",
                max(0, _base_est),
            )

        # Write the system message ONLY on the freeze turn; on frozen turns
        # it stays byte-identical so the provider prompt cache survives.
        if not _is_frozen_turn:
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
            _traj_sink = getattr(self.session, "trajectory_sink", None)
            if callable(_traj_sink):
                with contextlib.suppress(Exception):
                    _traj_sink("system.prompt_frozen", {"chars": len(system_prompt)})

        # Add user prompt if not a duplicate of the last user message
        # in the active branch context (not just self.session.messages[-1],
        # which may be from a different branch during retry).
        # The compaction overlay (get_context_messages) must NOT be used for
        # this check: its tail can be a summary / rewritten message whose
        # content differs from the raw prompt, which silently defeated this
        # dedup and made every run of a compacted session append the user
        # prompt a SECOND time (the duplicated user bubbles bug).
        # Skip synthetic [CONTEXT UPDATE] injections: they are harness-
        # emitted user-role messages, and treating one as "the last user
        # message" made the real prompt compare against foreign content and
        # get appended a SECOND time right after every injection.
        # IMPORTANT: scan the MERGED branch context (parent lineage + own
        # messages), not the raw per-branch message list.  A retry fork point
        # lands ON a user message that stays owned by the PARENT branch, so a
        # raw scan over ``branch_id == active`` finds nothing in the fresh
        # child branch and appends the prompt a SECOND time -- the model then
        # sees the same question twice and answers "you already asked this".
        # get_branch_messages(sanitize_for_model=False) is still the raw
        # persisted view, NOT the compaction overlay, so the overlay-vs-raw
        # concern below does not apply here.
        last_raw_user = None
        for m in reversed(self.session.get_branch_messages(
            self.session.active_branch_id, sanitize_for_model=False
        )):
            if m.get("role") == "user" and not m.get("is_synthetic"):
                last_raw_user = m
                break
        # An empty prompt means the caller already staged the user turn
        # itself (the multimodal path stores a content-array message and
        # hands the loop an empty string).  Comparing that array against ""
        # always mismatched, so the loop appended a SECOND, empty user
        # message after every image turn -- shifting the stored ordinals
        # that message-level rollback and delete resolve against.
        _staged_by_caller = prompt == "" and last_raw_user is not None
        if last_raw_user is None or (
            last_raw_user.get("content") != prompt and not _staged_by_caller
        ):
            if skill_prompt and last_raw_user is not None:
                # Skill was activated -- don't add a duplicate with the stripped text.
                # Keep the original message content so the user sees what they typed.
                pass
            else:
                logger.info(
                    "[sub_agent] adding user message to session | prompt_len=%s | last_ctx_user_exists=%s",
                    len(prompt),
                    last_raw_user is not None,
                )
                self.session.add_message("user", prompt)
                # Flush the user message to disk before entering the model
                # loop so a process kill here still leaves a resumable
                # transcript.  Mirrors Claude Code QueryEngine.ts:450-463.
                with contextlib.suppress(Exception):
                    await self.hook_system.emit_user_message_persisted(self.session.id or "")
                self.event_stream.publish(
                    UserMessagePersisted(
                        session_id=self.session.id or "",
                        content=prompt,
                    )
                )

        if time.time() - _t0 > 0.1:
            logger.info("[perf] prompt build %.1fs", time.time() - _t0)

        # Complementary system-prompt engineering: plugin sections are
        # merged AROUND the built-in body (priority-sorted, appended after
        # it so the core identity/rules stay authoritative and the cache
        # prefix is untouched).  Neither side replaces the other; a broken
        # plugin contributes nothing at all.
        try:
            from encre.kernel.services import shared_plugin_registry_raw as _get_shared_plugin_registry_raw

            _reg = _get_shared_plugin_registry_raw()
        except Exception:
            _reg = None
        if _reg is not None:
            _sections: list[str] = []
            _hosts: list = []
            _agent_host = getattr(_reg, "agent_host", None)
            if _agent_host is not None:
                _hosts.append(_agent_host)
            _shared_host = getattr(_reg, "host", None)
            if _shared_host is not None and _shared_host not in _hosts:
                _hosts.append(_shared_host)
            for _h in _hosts:
                try:
                    _sections.extend(_h.collect_system_prompt_sections())
                except Exception:
                    logger.debug("[run] plugin system-prompt section failed", exc_info=True)
            _extra = "\n\n".join(dict.fromkeys(_sections))
            if _extra:
                system_prompt = f"{system_prompt}\n\n{_extra}" if system_prompt else _extra

        t.system_prompt = system_prompt
        # Re-snapshot AFTER the user prompt has been persisted above: the
        # earlier ``ctx_msgs`` snapshot was taken before the dedup check, so
        # assigning it directly would send the first model request of the run
        # to the backend without the user's message.  For sub-agents (which
        # pre-add the prompt) this re-fetch returns the identical cached list.
        t.context_msgs = self.session.get_context_messages()

        # ── Context usage statistics ─────────────────────────────────
        # Per-request token composition for the frontend context-usage
        # display (system prompt / messages / tools by connector kind /
        # skills).  Measured AFTER the final system prompt is assembled
        # and the user prompt persisted, so the numbers match what the
        # next model call actually sends.
        with contextlib.suppress(Exception):
            _skills_parts = (
                skill_prompt or "",
                doc_skills_prompt or "",
                tool_skills_prompt or "",
                (_catalogue.content if _catalogue is not None else ""),
            )
            t.context_usage = compute_context_usage(
                system_prompt=system_prompt,
                messages=t.context_msgs,
                tools=tools,
                model=self.config.model,
                context_window=_ctx_window,
                skills_text="\n\n".join(p for p in _skills_parts if p),
            )
            self.telemetry.record_context_usage(t.context_usage)
            # Publish immediately: the composition is fixed at request-build
            # time, so mid-turn protocol pushes (tool-call events) can carry
            # the current turn's real figures without waiting for Finish.
            with contextlib.suppress(Exception):
                self.last_context_usage = t.context_usage
                self.session.metadata["context_usage"] = t.context_usage
        # NOTE: plugin context-provider injection moved to the model phase --
        # providers must contribute FRESH messages before EVERY model call,
        # but this prompt phase only runs once per run() while TurnContext
        # (and its context_msgs) is reset per turn.

    async def _collect_session_recall_fragments(self, query: str) -> list[RetrievedFragment]:
        """Complementary recall: plugin stores MERGED with BM25 transcript index.

        Two complementary sources, both consulted every turn and merged by
        score (doc-id deduped, current session excluded):

        * Plugin memory stores (``search(query, top_k)``) -- semantic or
          external knowledge the transcript index does not have.  Every
          call is guarded and timeout-bounded on the host side; a failure
          simply contributes no fragments.
        * The built-in BM25 index over past session transcripts (lazy,
          failure-tolerant) -- history no external store has seen.  The
          index is built/refreshed on first use and reused in-process;
          :meth:`SessionMemoryIndex.build` is signature-checked, so
          unchanged sessions never re-scan.

        Args:
            query: The user prompt driving this turn.

        Returns:
            Up to three retrieved fragments (may be empty).
        """
        try:
            # Complementary recall: plugin memory stores and the built-in
            # BM25 transcript index are BOTH consulted on every turn and
            # their fragments are MERGED by score.  Plugin stores contribute
            # knowledge the transcript index does not have; the built-in
            # index covers session history no external store has seen.
            # Plugin stores may return plain dicts; normalize everything to
            # RetrievedFragment so the context compiler never sees a
            # heterogeneous list (it renders dataclass attributes only).
            merged: list[RetrievedFragment] = []
            _seen_docs: set[str] = set()

            def _absorb(frags: list) -> None:
                for fr in frags[:6]:
                    if isinstance(fr, RetrievedFragment):
                        norm = fr
                    elif isinstance(fr, dict):
                        norm = RetrievedFragment(
                            doc_id=str(fr.get("doc_id") or fr.get("id") or "plugin"),
                            score=float(fr.get("score", 0.0) or 0.0),
                            snippet=str(fr.get("snippet") or fr.get("content") or fr.get("text") or ""),
                        )
                    else:
                        continue
                    if norm.doc_id not in _seen_docs:
                        _seen_docs.add(norm.doc_id)
                        merged.append(norm)

            try:
                from encre.kernel.services import shared_plugin_registry_raw as _get_shared_plugin_registry_raw

                _reg = _get_shared_plugin_registry_raw()
            except Exception:
                _reg = None
            if _reg is not None:
                _hosts: list = []
                _agent_host = getattr(_reg, "agent_host", None)
                if _agent_host is not None:
                    _hosts.append(_agent_host)
                _shared_host = getattr(_reg, "host", None)
                if _shared_host is not None and _shared_host not in _hosts:
                    _hosts.append(_shared_host)

                # Both hosts are recalled CONCURRENTLY when more than one is
                # wired: their inner providers already run in parallel, so a
                # serial host loop would double the worst-case recall latency.

                async def _recall_host(_h: Any) -> list:
                    try:
                        return await _h.collect_memory_fragments(query, top_k=3)
                    except Exception:
                        logger.debug("[run] plugin memory store failed", exc_info=True)
                        return []

                for _frags in await asyncio.gather(*(_recall_host(_h) for _h in _hosts)):
                    _absorb(_frags)

            idx = _shared_session_memory_index()
            # Building walks every session on disk; keep it off the event loop
            # so the turn's first token is not delayed by transcript scanning.
            await asyncio.to_thread(idx.build)
            if query and query.strip():
                _absorb(list(idx.search(query, top_k=3)))

            merged.sort(key=lambda f: f.score, reverse=True)
            return [fr for fr in merged[:3] if fr.doc_id != (self.session.id or "")]
        except Exception:
            logger.debug("[run] session recall failed", exc_info=True)
            return []


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
        # (recall, clarify, disclose, precedence, rule interpretation) right
        # where the model begins responding; the full text still lives in the
        # system prompt.
        #
        # ``work`` register only.  In ``chat`` the reminder would attach six
        # task gates to a greeting -- which is precisely how conversation got
        # answered as a deliverable.  In ``answer`` the recall/clarify discipline
        # is already carried by the system-prompt blocks that register keeps.
        t.standing_orders_text = build_standing_orders_reminder() if (not t.skip_enrichment and t.register == "work") else ""
        t.standing_orders_injected = False

        # Checkpoint hard-gate state: injected once per run() when the number
        # of consecutive tool-call steps (since the last user message) crosses
        # the threshold AND the user has not explicitly authorised a hands-off
        # run.  The injected message hands control back to the user.
        #
        # This is the ONE place the pause policy lives.  It used to be stated
        # four times (mandatory_constraints Gate 4, task_completion's "Boundary
        # of Keep Going", standing-orders rule 3, sub-agent enforcement) on top
        # of the code gate here, and the prompt copies -- being closer to the
        # model and phrased as user messages -- out-weighed the system prompt's
        # "finish the job" instruction.  The prompt now only speaks when the
        # gate actually fires.
        t.checkpoint_injected_this_run = False
        t.checkpoint_text = ""

        # Sanitize session messages on every run -- old sessions loaded from
        # disk may contain broken tool_call groups (from crashes) that cause
        # 400 errors.  Only sanitize active branch context; other branches
        # remain untouched.  The sanitized list is installed as the
        # model-context overlay (never persisted back over the branch
        # history): turn files keep every message, including the broken ones.
        active_branch_id = loop.session.active_branch_id
        if active_branch_id not in loop._sanitized_branches:
            sanitized = loop.compact_engine.sanitize(t.context_msgs)
            if len(sanitized) < len(t.context_msgs):
                _upto = max(
                    (m.get("seq_in_branch", -1) for m in t.context_msgs),
                    default=-1,
                )
                loop.session.apply_compacted_view(active_branch_id, sanitized, _upto)
            loop._sanitized_branches.add(active_branch_id)
            t.context_msgs = loop.session.get_context_messages()
        t.active_branch_id = active_branch_id
        return PhaseResult.continue_()
