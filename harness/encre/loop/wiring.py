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

"""Constructor wiring mixin for :class:`~encre.loop.runner.EncreLoop`.

Every long-lived collaborator (backend, safety engine, compaction pipeline,
plan-mode manager, skill manager, working-set manager, sub-agent runner,
error orchestrator, evolution components, event backbone, ...) is built here
exactly once.  The typed :class:`~encre.events.EventStream` is projected over
the single append-only :class:`~encre.events.EventSourcedBus` backbone (Task
4.1/4.3 of the architecture refactor).

Because it is the collaborator that owns all of that state, this mixin also
owns :meth:`_LoopWiringMixin.reload` -- re-applying a new configuration to a
live loop by refreshing the state derived from it (mode profile, rules, skills,
learned policies, ...), *without* rebuilding the collaborators themselves.  See
:mod:`encre.loop.hot_reload` for the boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from encre.backend import create_backend as _create_backend_impl
from encre.compact.engine import CompactEngine
from encre.compact.loop_state.manager import StateManager
from encre.compact.loop_state.state import LoopTurnState
from encre.compact.pipeline import CompactionPipeline
from encre.config import EncreConfig
from encre.events import EventSourcedBus, EventStream
from encre.features import adapters as _features
from encre.hooks.system import EncreHookSystem
from encre.logging_config import get_logger
from encre.loop.phase_registry import PhaseRegistry
from encre.loop.phases import (
    EndOfTurnPhase,
    ExecuteToolsPhase,
    ModelPhase,
    PostStreamPhase,
    PostToolCompactPhase,
    PreModelCompactPhase,
    PrepareToolsPhase,
    SecondaryToolsPhase,
    SessionStartPhase,
    SystemPromptPhase,
)
from encre.loop.state import TURN_INTERRUPTED_EVENT_TYPE, LoopStateMachine
from encre.loop_commands import CommandManager
from encre.loop_context import ContextBuilder
from encre.loop_error import ErrorOrchestrator
from encre.loop_skills import SkillManager
from encre.loop_stability import BudgetState, SteerQueue
from encre.loop_sub_agent import SubAgentRunner
from encre.loop_working_set import WorkingSetManager
from encre.modes.loop_plan_mode import PlanModeManager
from encre.modes.mode_profiles import AgentMode, get_mode_profile
from encre.profile.system import EncreProfileSystem
from encre.prompts.base import EncrePromptTemplate
from encre.recovery import ErrorRecoveryEngine, IdempotencyLedger
from encre.rollback import EncreRollbackGit
from encre.rules.loader import RulesLoader
from encre.safety import EncreSafetyEngine
from encre.safety_policy import EncreSafetyPolicyReflow
from encre.session import EncreSession
from encre.skills.registry import EncreSkillRegistry
from encre.soul.system import EncreSoulSystem
from encre.telemetry import EncreTelemetry
from encre.thinking.config import resolve_thinking_config
from encre.tools.discovery import ToolDiscovery
from encre.tools.registry import ToolRegistry
from encre.tracing import maybe_get_tracer, setup_tracing
from encre.utils.verification_ledger import VerificationLedger

if TYPE_CHECKING:
    # Annotation-only imports: ``ReloadReport`` because ``reload()`` pulls the
    # hot-reload machinery in lazily, and the feature-package types because the
    # loop never imports ``improve``/``memdir``/``device`` at runtime -- it
    # resolves them through :mod:`encre.features.adapters`, the declared
    # feature seam.  Imports under ``TYPE_CHECKING`` are not dependency edges.
    from encre.improve.evolution.config import EvolutionConfig
    from encre.improve.feedback.learner import EncreFeedbackLearner
    from encre.loop.hot_reload import ReloadReport
    from encre.memdir.curator import MemoryCurator
    from encre.memdir.layers import MemoryLayerStore
    from encre.memdir.recall import RecallEngine
    from encre.memdir.system import EncreMemorySystem

logger = get_logger(__name__)


class _LoopWiringMixin:
    """Builds and wires every collaborator of the agent loop."""

    def __init__(
        self,
        config: EncreConfig,
        session: EncreSession,
        tool_registry: ToolRegistry | None = None,
        hook_system: EncreHookSystem | None = None,
        safety: EncreSafetyEngine | None = None,
        memory_system: EncreMemorySystem | None = None,
        profile_system: EncreProfileSystem | None = None,
        soul_system: EncreSoulSystem | None = None,
        skill_registry: EncreSkillRegistry | None = None,
        telemetry: EncreTelemetry | None = None,
        evolution: EvolutionConfig | None = None,
        recovery: ErrorRecoveryEngine | None = None,
        feedback: EncreFeedbackLearner | None = None,
        sub_agent_depth: int = 0,
        mode: AgentMode | str | None = None,
    ) -> None:
        """Construct the loop and wire up all collaborating subsystems.

        Optional arguments fall back to freshly constructed defaults when
        ``None`` so a loop can be created with minimal explicit wiring.

        Args:
            config: The Encre configuration controlling backend, model,
                thinking, tracing, token budget and more.
            session: The persistent session this loop runs against.
            tool_registry: Optional tool registry; a new one is created if
                omitted.
            hook_system: Optional hook system; a new one is created if omitted.
            safety: Optional safety engine; a default is built from ``config``.
            memory_system: Optional memory system, used to enrich context.
            profile_system: Optional profile system.
            soul_system: Optional "soul"/persona system.
            skill_registry: Optional skill registry; activates skill support.
            telemetry: Optional telemetry; defaults to a disabled instance.
            evolution: Optional evolution configuration; defaults are applied.
            recovery: Optional error recovery engine; a default is used.
            feedback: Optional feedback learner.
            sub_agent_depth: Nesting depth for sub-agents; the top loop is 0.

        Returns:
            None.
        """
        self.config = config
        self.session = session
        self.tool_registry = tool_registry or ToolRegistry()
        self.discovery = ToolDiscovery(self.tool_registry)
        self.hook_system = hook_system or EncreHookSystem()
        self.memory_system = memory_system
        self.profile_system = profile_system
        self.soul_system = soul_system
        self.skill_registry = skill_registry
        self._skill_mgr = SkillManager(skill_registry)
        self.telemetry = telemetry or EncreTelemetry(enabled=False)
        # Expose telemetry on the session so session-level APIs (e.g.
        # add_tool_result's history redaction) can report out-of-band
        # counters into the same telemetry channel.
        self.session.telemetry = self.telemetry
        # Initialise OpenTelemetry tracer from config (no-op when disabled
        # or when opentelemetry-api is not installed).
        setup_tracing(
            enabled=config.tracing_enabled,
            service_name=config.tracing_service_name,
            endpoint=config.tracing_endpoint,
        )
        self._tracer = maybe_get_tracer()
        self.sub_agent_depth = sub_agent_depth
        # Tool-stop signalling: set by request_tool_stop() (ws "tool_stop"
        # message), awaited by the tool-execution sites so a long-running
        # tool can be aborted without killing the whole turn.
        self._tool_stop_event = asyncio.Event()
        # Capability profile: one engine, distinct per-mode behaviour.
        # Defaults to GENERAL (historical behaviour) when mode is unset.
        self.mode: AgentMode = AgentMode(mode) if isinstance(mode, str) and not isinstance(mode, AgentMode) else (mode or AgentMode.GENERAL)
        if not isinstance(self.mode, AgentMode):
            self.mode = AgentMode.GENERAL
        self._profile = get_mode_profile(self.mode)
        # Plan-Do-Review engine (WORKSPACE profile only): decomposes complex
        # tasks into a step graph, tracks per-step tool calls, and drives
        # light/deep review between steps.  The plan context is injected into
        # the system prompt every turn so the coder stays anchored to the
        # plan instead of drifting (mirrors Claude Code's plan-do-review).
        self._pdr = _features.PlanDoReviewEngine()
        self._pdr_active = False
        evo = evolution or _features.EvolutionConfig.create_default()
        self.learner = evo.learner
        self.optimizer = evo.optimizer
        self.reflex = evo.reflex
        self.meta = evo.meta
        self.reviewer = evo.reviewer
        self.event_store = evo.event_store
        # Load persisted evolution/learning state so past experience survives
        # restarts (previously never loaded, so every restart started empty).
        for _comp in (self.learner,):
            _load = getattr(_comp, "load", None)
            if _load is not None:
                with contextlib.suppress(Exception):
                    _load()
        self.recovery_engine = recovery or ErrorRecoveryEngine()
        # Tier-aware recovery: the engine refuses blind same-call retries
        # for light-tier models (probe resolves lazily so model switches
        # and hot-reloads are picked up through the profile memo).
        self.recovery_engine.set_tier_probe(
            lambda: self._model_profile().is_light
        )
        # Idempotency journal for guarded tool calls (write tools): shared by
        # every turn of this session, persisted next to the session's other
        # loop-state files.  Crash residue (an "unknown" record) makes the
        # executor refuse a blind re-run instead of risking double-apply.
        self._idempotency = IdempotencyLedger(
            Path("data", "sessions", getattr(self.session, "id", "") or "unknown"),
        )
        # Wire event store to hook system for automatic lifecycle recording
        if self.event_store is not None and evo.event_store_enabled:
            self.event_store.wire_hooks(self.hook_system)
        # Cached microcompact state (per-session).  Lazily initialised on the
        # first turn when the backend is Anthropic so non-Anthropic backends
        # pay zero cost.  Mirrors Claude Code's ``CachedMCState``.
        self._cache_edits_state: Any = None
        self.feedback = feedback
        _fback_load = getattr(self.feedback, "load", None)
        if _fback_load is not None:
            with contextlib.suppress(Exception):
                _fback_load()
        # Tracks the currently-streaming (not yet committed) assistant turn so a
        # cancel / hard-exit can persist the partial text/thinking that would
        # otherwise be lost on refresh (the backend only commits an assistant
        # message when a text block completes or tool calls are declared).
        self._pending_stream_text: list[str] = []
        self._pending_stream_thinking: list[str] = []

        # Context renderer for tracking what changed between turns.
        from encre.context.renderer import ContextRenderer

        self._ctx_renderer = ContextRenderer()
        # Auto-resolve thinking config based on model if not explicitly set.
        # Per-model config takes precedence over the global config.
        active_model = config.get_active_model()
        self._thinking_config = active_model.thinking_config or config.thinking_config
        if self._thinking_config is None:
            self._thinking_config = resolve_thinking_config(None, config.model, backend_type=config.backend_type)
        # apply_active_model() (called by EncreConfig.from_file) may have
        # injected context_window into backend_kwargs; drop it so the
        # explicit kwarg below cannot collide.  The sampling keys are
        # spread explicitly, so a stale backend_kwargs copy is dropped too.
        _sampling = config.active_model_sampling_overrides()
        _backend_kwargs = {
            k: v
            for k, v in config.backend_kwargs.items()
            if k != "context_window" and k not in _sampling
        }
        # Resolve the factory through the ``encre.loop`` package namespace so
        # the documented patch point (``encre.loop.create_backend``) works for
        # tests and embedding code; fall back to the direct import when the
        # package attribute is unavailable (partial initialisation).
        import encre.loop as _loop_pkg

        backend_factory = getattr(_loop_pkg, "create_backend", None) or _create_backend_impl
        self.backend = backend_factory(
            config.backend_type,
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            models=config.models,
            thinking_config=self._thinking_config,
            # Honour the active model's resolved context window from the
            # first turn -- otherwise the backend falls back to its built-in
            # default and compaction thresholds ignore model switches made
            # in a previous app run.
            context_window=getattr(active_model, "context_window", 0) or 0,
            # Custom provider headers must be applied on the initial backend
            # as well, matching EncreAgent.rebuild_backend().
            extra_headers=getattr(active_model, "headers", None) or None,
            # The model's sampling overrides must be stamped on the INITIAL
            # backend too (not just after rebuild_backend on a settings
            # change), or relay servers that force-require temperature
            # reject every turn of a freshly loaded session.
            **_sampling,
            **_backend_kwargs,
        )
        self.safety = safety or EncreSafetyEngine(config, workspace=str(getattr(config, "workspace", "") or ""))
        self.compact_engine = CompactEngine()
        # Share the loop's engine with the pipeline so circuit-breaker
        # counts, cooldowns and the pre-compact cache stay in one place.
        self._compaction_pipeline = CompactionPipeline(self.compact_engine)
        self.prompt_builder = EncrePromptTemplate()
        self.rollback = EncreRollbackGit()
        self._cancel_event = asyncio.Event()
        # Active sub-agent loops spawned by THIS loop.  When the user hits
        # the Stop button on the parent, we cancel every child here so a
        # single click terminates the entire agent tree immediately,
        # not just the top-level loop.
        self._child_loops: set[Any] = set()
        # Plan-mode state. ``plan_mode_active`` is a *derived* read-only
        # property (True iff ``config.slash_command_mode == "plan"``) so the
        # boolean flag can never drift out of sync with the string mode the
        # rest of the system reads.  When plan mode is on, write-class tools
        # (``file_write``/``file_edit``/``apply_patch``) are NOT executed
        # directly.  Instead the loop builds a preview (diff/command
        # summary), emits a ``PlanProposal`` event, and waits for the user
        # to approve or reject via ``approve_plan``/``reject_plan`` before
        # continuing.  This gives desktop UI a real "plan-first" workflow
        # that matches Claude Code's plan mode.
        # All mode transitions go through :meth:`set_mode`, which keeps the
        # ``config.slash_command_mode`` string, ``session.metadata`` mirror,
        # and the derived ``plan_mode_active`` flag consistent atomically.
        self._state_mgr = StateManager(self.session)
        self._cmd_mgr = CommandManager(self.config, self._state_mgr)
        self._plan_mode = PlanModeManager(
            config=self.config,
            state_mgr=self._state_mgr,
            safety=self.safety,
            cancel_event=self._cancel_event,
        )
        self._rules_loader = RulesLoader()
        self._recent_tool_names: list[tuple[str, ...]] = []  # tool_name:args_sig signatures
        self._error_tool_names: set[str] = set()
        # Anti-stuck guardrail ladder state: per canonical signature counts and
        # per-idempotent-tool last result digest for no-progress detection.
        # Counts are *consecutive-turn* counters (a signature only accumulates
        # when it recurs on adjacent turns), so a common tool used legitimately
        # on scattered turns never accumulates a false count.
        self._guardrail_call_counts: dict[str, int] = {}
        self._guardrail_prev_sigs: set[str] = set()
        self._guardrail_no_progress_digests: dict[str, str] = {}
        self._guardrail_no_progress_counts: dict[str, int] = {}
        # When a turn is hard-halted by the guardrail this is set True and the
        # main loop breaks out (opt-in circuit breaker, see _GUARDRAIL_HALT_AFTER).
        self._guardrail_halt = False
        # Set True when at least one tool this turn committed an undo entry
        # (i.e. had side effects); drives the single post-turn write
        # checkpoint in the runner.
        self._writes_this_turn = False
        # Verify-on-stop (Hermes port): a bounded nudge counter to prevent
        # deadlock, plus a verification ledger that tracks per-file evidence so
        # the nudge is evidence-driven (never-checked vs checked-and-failed)
        # rather than a blind reminder.
        self._verify_on_stop_nudges = 0
        self._verif_ledger = VerificationLedger()
        # Forced-review escalation (review stage of the loop): counts how many
        # times we injected a critical-review demand after verify nudges were
        # exhausted but a check still failed.  Bounded by _MAX_FORCED_REVIEWS.
        self._forced_review_count = 0
        # Hard verification gate: the last line of defence before a text-only
        # finish is allowed.  Once verify-on-stop nudges and the forced-review
        # stage are exhausted, this counter escalates the demands (project-level
        # regression: build + typecheck + lint + tests) before falling through
        # to the hard-gate fallback.  Bounded by _MAX_VERIFY_HARD_GATE.
        self._verify_hard_gate_count = 0
        # Diminishing-returns guard (Claude Code tokenBudget port): track
        # consecutive auto-continues whose output is tiny; stop continuing when
        # the model keeps producing near-empty output instead of real progress.
        self._auto_continue_consecutive = 0
        self._auto_continue_last_output = 0
        self._sanitized_branches: set[str] = set()
        # Device context manager — collects a lightweight device catalog for
        # L1 prompt injection and powers the device_* tools on demand.
        try:
            self._device_context_manager = _features.DeviceContextManager(config)
        except Exception:
            self._device_context_manager = None
        # Active tool set name.  Default is "default"; changes when the mode
        # switches (e.g. plan mode → "plan") or when the user explicitly sets it.
        self._tool_set_name: str = self._resolve_tool_set_for_mode()
        self._ctx_bldr = ContextBuilder(
            self.config,
            self.session,
            cache_fresh=self._cache_fresh,
            memory_system=self.memory_system,
            soul_system=self.soul_system,
            profile_system=self.profile_system,
            git=getattr(self, "git", None) or getattr(self, "_git", None),
            rules_loader=self._rules_loader,
            device_context_manager=self._device_context_manager,
        )
        # Background compaction task -- runs in parallel to avoid blocking the main loop
        self._compact_task: asyncio.Task[None] | None = None
        # Compaction epoch: incremented every time a NEW compaction pass is
        # triggered (background or synchronous).  A running background task
        # records the epoch it was launched with; when it finishes it only
        # replaces the branch messages if no newer compaction pass started in
        # the meantime.  This replaces the old cancel-and-restart behaviour,
        # which threw away the summary LLM call every turn and, when the
        # compaction was slow (near-limit context), meant compaction never
        # completed while the context kept growing.
        self._compact_epoch: int = 0
        # Pending compact notification to yield at next turn start
        self._compact_notification = None
        # Whether any compaction (synchronous or background) replaced messages
        # during the current turn.  The Finish event carries this flag so the
        # frontend can request a message refresh and avoid "Message not found"
        # errors when rolling back to compacted-away messages.
        self._compacted_this_turn: bool = False
        # Streaming tool execution cache: maps client_id → precomputed execution result.
        # Populated in background during streaming when
        # ``enable_streaming_tool_execution`` is True.
        self._streaming_tool_results: dict[str, dict[str, Any]] = {}
        # Strong references to the fire-and-forget streaming pre-execution
        # tasks so they are not garbage-collected mid-run (RUF006).
        self._streaming_tool_tasks: set[asyncio.Task[None]] = set()
        # Client ids of the tool calls those tasks are running, so the tool
        # phase can announce "running" for them before it drains the tasks.
        self._streaming_tool_clients: set[str] = set()
        # Background sub-agent tracker for async/fire-and-forget mode.
        # Lazily initialised on first async sub-agent spawn.
        self._bg_sub_agents: Any = None
        # --- Recovery state ------------------------------------------------------
        # Unified error orchestrator — owns all recovery decisions and
        # counters.  Replaces the old RecoveryStateMachine + inline scalars.
        self._max_output_tokens_override: int | None = None
        self._error_orch: ErrorOrchestrator = ErrorOrchestrator(
            budget=self.recovery_engine.budget,
        )
        # Per-run loop state (initialized at start of each _run_impl).
        self._state: LoopTurnState | None = None
        # Fallback model tracking: set when a fallback switch occurs.
        self._active_fallback_model: str = ""
        self._active_fallback_backend_type: str = ""
        # Model ids already tried this session (for unified model fallback).
        self._fallback_tried: set[str] = set()
        # Rate-limit cooldowns: model_id -> timestamp when the model becomes
        # cycle-eligible again (see model_pool_cooldown_ms).
        self._pool_cooldowns: dict[str, float] = {}
        # Reactive compact guard: set to True after first reactive compact per turn.
        self._has_attempted_reactive_compact: bool = False
        # System prompt cache: keyed by content hash so we skip rebuild when nothing changed.
        self._sys_prompt_cache: str | None = None
        self._sys_prompt_cache_key: Any = None
        # Spec engine: set externally by ws.py so the loop can parse specs
        # and enforce the approval gate in spec mode.
        self.spec_engine: Any = None
        # Steer queue for mid-conversation user instructions
        self._steer_queue: SteerQueue = SteerQueue()
        # submit_review state: the tool blocks the turn on the user's
        # review decision; resolve_review() wakes it.  _current_review is
        # the record built at submit time (mode + artifact paths).
        self._review_event: asyncio.Event | None = None
        self._review_decision: tuple[bool, str] | None = None
        self._current_review: Any = None
        # Review record closed by ``_finalize_cancelled_turn``; the run loop
        # echoes it once so the card stops offering a dead decision.
        self._pending_review_notice: Any = None
        # Loop runtime state machine ("what is the loop waiting on?") backed
        # by an append-only event store.  It shares the session's
        # ``events.jsonl`` with TrajectoryRecorder (disjoint event-type
        # namespaces; every append re-syncs the sequence from the file tail,
        # so the co-located writers never desynchronise).  The path mirrors
        # the TrajectoryRecorder convention (``data/sessions/<id>``) so both
        # writers land on the same file.  A stale ``waiting_*``/``compacting``
        # state left behind by a previous crashed process is recovered back
        # to ``running`` here, with the recovery recorded as a
        # ``state_recovered`` event (approval gates are NOT skipped -- the
        # live flow simply re-asks).
        self._loop_sm = LoopStateMachine(
            Path("data", "sessions", getattr(self.session, "id", "") or "unknown"),
        )
        self._loop_sm.recover_from_crash()
        # Phase journal reconciliation: if the previous process completed
        # phases of a turn without ever closing it (hard crash mid-turn),
        # surface it loudly and persist the marker.  No magic resume -- the
        # session history is consistent, the next turn simply continues.
        _incomplete_turn = self._loop_sm.store.last_incomplete_turn()
        if _incomplete_turn is not None:
            logger.warning(
                "[loop-wiring] previous process died mid-turn: turn=%s last_phase=%s phases_completed=%s",
                _incomplete_turn.get("turn"),
                _incomplete_turn.get("last_phase"),
                _incomplete_turn.get("phases_completed"),
            )
            self._loop_sm.record(TURN_INTERRUPTED_EVENT_TYPE, _incomplete_turn)
        # Expose the state machine to the session so session-level APIs (e.g.
        # ``EncreSession.undo``) can append undo events to the same event
        # stream without holding a back-reference to the loop.
        self.session.loop_state_machine = self._loop_sm
        # Thread-safe steering inbox: UI / protocol threads push mid-run user
        # messages here via ``enqueue_user_message``; the loop drains them at
        # turn boundaries (runner) and tool-execution boundaries (secondary
        # tools phase) via ``drain_pending_messages``.
        self._steering_inbox: deque[dict[str, Any]] = deque()
        self._steering_inbox_lock = threading.Lock()
        # Budget state for grace call support.  Restored from session metadata
        # so a restarted session resumes its accrued token budget instead of
        # resetting to zero (mirrors Claude Code's task_budget accrual across
        # compact boundaries / session restarts).
        self._budget_state: BudgetState = BudgetState.restore(
            self.session.metadata.get(BudgetState.META_KEY),
            fallback_max=getattr(self.config, "token_budget", 0),
        )
        # Thinking prefill toggle
        self._thinking_prefill_enabled: bool = getattr(self.config, "thinking_prefill_enabled", False)
        self._working_set = WorkingSetManager(
            session=self.session,
            state_mgr=self._state_mgr,
            config=self.config,
        )
        self._sub_agent_runner = SubAgentRunner(
            config=self.config,
            tool_registry=self.tool_registry,
            memory_system=self.memory_system,
            profile_system=self.profile_system,
            soul_system=self.soul_system,
            skill_registry=self.skill_registry,
            hook_system=self.hook_system,
            safety=self.safety,
            sub_agent_depth=self.sub_agent_depth,
            child_loops=self._child_loops,
            session=self.session,
            mode=self.mode,
        )
        # ── Typed event infrastructure (architecture refactor 4.1/4.3) ──
        # ``event_bus`` is the single append-only event backbone for this
        # session, persisted under the project data directory.  It carries the
        # named-phase hook semantics (the historical ``pre_model_request``
        # hook is registered on it below).  ``event_stream`` is the typed
        # fan-out point projected over that backbone: every AgentEvent yielded
        # by ``run()`` is published here so plugins / telemetry can tap the
        # loop without touching the transport layer.
        session_id = getattr(self.session, "id", "") or ""
        ledger_path = self._event_ledger_path(session_id)
        self.event_bus: EventSourcedBus = EventSourcedBus(
            "events",
            ledger_path=ledger_path,
        )
        # Expose the backbone on the session so state collaborators (e.g.
        # :class:`~encre.compact.loop_state.manager.StateManager`) can publish
        # their changes without a hard dependency on the loop.
        self.session.event_bus = self.event_bus
        self.event_stream: EventStream = EventStream(
            "agent",
            bus=self.event_bus,
            session_id=session_id,
        )
        self.event_bus.on("pre_model_request", self._emit_pre_model_request_hook)
        # ── Plugin activation-event bridge (lazy plugins) ──
        # Plugins declaring ``activation_events`` stay dormant at startup
        # and are imported + activated when a matching lifecycle event
        # fires.  ``session_start`` is emitted by the agent at the end of
        # ``load_plugins`` (so it fires after discovery + tool injection,
        # also in deferred-load mode); the loop fires ``turn_end``,
        # ``session_end`` and ``mode_changed:<mode>`` below.

        # ── Layered memory + associative recall (Task 4.5) ──
        # ``memory_layers`` is the single durable home for the persistent
        # cognition the significance judge produces: attaching it to the
        # backbone turns ``memory_candidate`` events into real memories
        # instead of leaving the judgment stage "spinning idle".  Recall is
        # exposed through the same store so tools / context builders can
        # reach associative retrieval.  Construction is defensive (a memory
        # fault must never stop the loop from being built) but never silent:
        # failures are logged with their traceback.
        self._skill_memory_verifiers: set[int] = set()
        self.memory_layers: MemoryLayerStore | None = None
        self.recall_engine: RecallEngine | None = None
        self.memory_curator: MemoryCurator | None = None
        self._wire_memory_layers()

        # ── Learned safety-policy reflow (Task 4.1) ──
        # ``EncreSafetyPolicyReflow`` compiles the feedback learner's
        # corrections into tightening-only permission policies and pushes them
        # into the Rust permission engine -- the single source of truth for
        # authorization.  It is built here rather than in :mod:`encre.agent`
        # because it needs the event backbone, which only exists once the loop
        # has been wired.  Construction is defensive (a reflow fault must never
        # stop the loop from being built) but never silent.
        self._policy_reflow: EncreSafetyPolicyReflow | None = None
        # Record count observed at the previous reflow run.  ``-1`` means "never
        # run", so the first turn reconciles the live learner with the policy
        # document even when no correction arrived during this session.
        self._last_reflow_record_count: int = -1
        self._wire_policy_reflow()

        # ── Pluggable phase pipeline (P1.1) ──
        # The registry holding every pipeline phase; the runner iterates it
        # instead of a hard-coded call sequence.  Built last so every
        # collaborator the phase adapters reach through the loop exists.
        self.phase_registry: PhaseRegistry | None = None
        self._wire_phase_registry()

    def _wire_phase_registry(self) -> None:
        """Register the built-in phase pipeline on the loop's registry.

        The registration order below is byte-for-byte the call order the
        runner used to hard-code in ``_run_impl``: run-scoped phases first
        (``system_prompt`` -> ``session_start``), then the per-turn sequence
        (``pre_model_compact`` -> ``model`` -> ``post_stream`` ->
        ``prepare_tools`` -> ``execute_tools`` -> ``secondary_tools`` ->
        ``post_tool_compact`` -> ``end_of_turn``).

        Customisation is reversible and side-effect clean: embedders replace
        a built-in by registering a phase with the same ``name`` (the swap
        keeps the pipeline position) or remove one with ``unregister`` --
        the loop tolerates a missing optional phase.  Idempotent by guard:
        a second call never re-registers, so an already-customised registry
        is never clobbered by re-wiring.
        """
        if self.phase_registry is not None:
            # Already wired: re-registering would overwrite any phase the
            # embedder swapped in after construction.
            return
        registry = PhaseRegistry()
        for phase in (
            SystemPromptPhase(self),
            SessionStartPhase(self),
            PreModelCompactPhase(self),
            ModelPhase(self),
            PostStreamPhase(self),
            PrepareToolsPhase(self),
            ExecuteToolsPhase(self),
            SecondaryToolsPhase(self),
            PostToolCompactPhase(self),
            EndOfTurnPhase(self),
        ):
            registry.register(phase)
        self.phase_registry = registry

    def _wire_policy_reflow(self) -> None:
        """Build the learned safety-policy reflow and restore prior policies.

        The reflow reads the feedback learner's corrections, compiles the ones
        that express a *permission* preference into tightening-only policies,
        persists them (encrypted, via :mod:`encre.secure_io`) and pushes them
        into the Rust permission engine.  Building it here -- after both the
        feedback learner and the event backbone exist -- is what makes the
        learner's corrections take effect as real authorization decisions; the
        turn-tail hook in :mod:`encre.loop.phases.end_of_turn` re-runs it
        whenever a new correction is recorded.

        At construction the persisted document is reloaded and re-pushed into
        the Rust engine (:meth:`EncreSafetyPolicyReflow.refresh`), so policies
        learned in a previous run are enforced before any tool call is
        authorized.  Every failure is logged with its traceback and swallowed:
        a reflow fault must never stop the loop from being built, but it is
        never hidden either.  When no feedback learner is present (the goal and
        task executors build the loop without one) no reflow is created and the
        turn-tail hook becomes a no-op.

        Returns:
            None.
        """
        if self._policy_reflow is not None:
            # Already wired: rebuilding would push a second copy of every
            # learned policy into the Rust engine.
            return
        if self.feedback is None:
            return
        try:
            reflow = EncreSafetyPolicyReflow(self.feedback, bus=self.event_bus)
        except Exception:
            logger.warning(
                "[wiring] learned safety-policy reflow unavailable",
                exc_info=True,
            )
            self._policy_reflow = None
            return
        self._policy_reflow = reflow
        # Expose the reflow on the session so collaborators can reach it
        # without a hard dependency on the loop.
        self.session.policy_reflow = reflow
        try:
            reflow.refresh()
        except Exception:
            logger.warning(
                "[wiring] failed to restore learned safety policies",
                exc_info=True,
            )

    # ── Plugin activation-event bridge ──────────────────────────────────────
    # Event vocabulary for manifest ``activation_events`` patterns:
    #   session_start / session_end  -- loop lifecycle (wiring / aclose)
    #   turn_end                     -- end of each completed turn
    #   mode_changed:<mode>          -- capability mode switch (set_mode)
    # Patterns support ``*``, exact names, ``category:*`` and ``prefix.*``.

    def _emit_plugin_event(self, event: str) -> list[str]:
        """Fire *event* into the plugin registry's lazy-activation machinery.

        Plugins declaring a matching ``activation_events`` pattern stay
        dormant at startup and are imported + activated when this fires.
        Their tool contributions are synced into the loop's tool registry
        right after, so they join the model-facing payload from the next
        turn.  Failures are logged and never interrupt the loop.
        """
        try:
            # Raw accessor: never trigger plugin discovery from here (in
            # deferred-load mode the lazy pool is simply empty -> no-op).
            from encre.kernel.services import shared_plugin_registry_raw as _get_shared_plugin_registry_raw

            registry = _get_shared_plugin_registry_raw()
            if registry is None:
                return []
            activated = registry.emit_activation_event(event, host=registry.default_host_dict())
        except Exception as exc:
            logger.warning("Plugin activation event '%s' failed: %s", event, exc)
            return []
        if activated:
            self._sync_activated_plugin_tools()
            logger.info("Lazy plugins activated on event '%s': %s", event, activated)
        return activated

    def _sync_activated_plugin_tools(self) -> None:
        """Register every activated plugin tool into the loop tool registry.

        Mirrors the agent's plugin-tool injection: registration IS
        activation, so a lazily activated plugin's tools must join the
        registry (and thus the model payload) immediately.
        """
        try:
            from encre.kernel.services import shared_plugin_registry_raw as _get_shared_plugin_registry_raw

            registry = _get_shared_plugin_registry_raw()
            if registry is None:
                return
            for tool in registry.get_all_tools():
                self.tool_registry.register(tool)
        except Exception as exc:
            logger.warning("Syncing lazily activated plugin tools failed: %s", exc)

    def _session_has_tool_activity(self) -> bool:
        """True once any assistant message in this session invoked tools.

        Mechanical transcript scan -- no intent/prompt-text classification:
        an assistant message carrying ``tool_calls``, or a recorded ``tool``
        role result.  Callers cache the result monotonically (history only
        grows, so once True it stays True).
        """
        try:
            for msg in self.session.get_context_messages():
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                if role == "assistant" and msg.get("tool_calls"):
                    return True
                if role == "tool":
                    return True
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
        return False

    def _wire_memory_layers(self) -> None:
        """Create the memory graph store + curator, bound to the event backbone.

        :class:`~encre.memdir.layers.MemoryLayerStore` owns the *persistent
        cognition* layer: everything the significance judge deems meaningful.
        It is the single durable store for memories, and the **only** retrieval
        path is :class:`~encre.memdir.recall.RecallEngine` over that store.
        The pre-existing :class:`~encre.memdir.system.EncreMemorySystem` is now
        pure file I/O: it is bound to this store as the ingestion front door
        (its ``refresh()`` ingests the ``.md`` memory directory), and its data
        (``<data>/memory/*.md``) is fed *into* the store rather than searched
        in parallel.  The :class:`~encre.memdir.curator.MemoryCurator` is the
        evolution half: it re-weights confidence and wires edges on turn end.

        ``attach`` is idempotent per bus, so rebuilding the wiring never
        subscribes twice.  Any failure is logged (with traceback) and leaves
        ``memory_layers`` / ``recall_engine`` as ``None`` -- the loop still
        constructs, but the real error is never hidden.
        """
        if self.memory_layers is not None:
            # Already wired: a second pass would attach a second store to the
            # same backbone and intake every candidate twice.
            return
        try:
            self.memory_layers = _features.MemoryLayerStore()
            self.memory_layers.attach(self.event_bus)
            self.recall_engine = _features.RecallEngine(self.memory_layers)
            self.memory_curator = _features.MemoryCurator(
                self.memory_layers,
                recall_engine=self.recall_engine,
            )
            self.memory_curator.attach(self.event_bus)
        except Exception:
            logger.warning(
                "[wiring] layered memory subsystem unavailable",
                exc_info=True,
            )
            self.memory_layers = None
            self.recall_engine = None
            self.memory_curator = None
            return
        # Expose both on the session so tools and the context builder can
        # reach associative recall without a hard dependency on the loop.
        self.session.memory_layers = self.memory_layers
        self.session.recall_engine = self.recall_engine
        self.session.memory_curator = self.memory_curator
        # The context builder is constructed *before* this subsystem exists, so
        # the recall collaborator is attached here rather than at construction.
        # When the subsystem is unavailable the builder keeps ``None`` and the
        # memory block of the prompt degrades to an empty string.
        self._ctx_bldr.recall_engine = self.recall_engine
        # The ``.md`` file facade is the ingestion front door: binding the store
        # to it turns the loop's ``memory_system.refresh()`` calls into real
        # ingestion of the memory directory.
        if self.memory_system is not None:
            try:
                self.memory_system.layer_store = self.memory_layers
            except Exception:
                logger.warning(
                    "[wiring] failed to bind the memory facade to the layer store",
                    exc_info=True,
                )

    def attach_skill_verifier(self, verifier: Any) -> None:
        """Bind a skill verification gate to procedural memory (Task 7.4).

        A verified, promoted skill is written into procedural memory via
        :meth:`_on_skill_promoted`, registered through
        ``verifier.on_promoted``.  That callback is the **only** write path:
        the ``skill_promoted`` bus event is deliberately not consumed as well,
        so a promotion is never recorded twice.  Rejected artifacts never fire
        the callback and therefore never reach memory.

        Idempotent per verifier instance and fully exception-isolated: a
        binding failure is logged, never raised, so skill promotion is
        unaffected.

        Args:
            verifier: A :class:`~encre.improve.verification.SkillVerifier`
                (duck-typed; anything exposing ``on_promoted`` works).
        """
        if verifier is None or self.memory_layers is None:
            return
        if id(verifier) in self._skill_memory_verifiers:
            return
        register = getattr(verifier, "on_promoted", None)
        if not callable(register):
            return
        self._skill_memory_verifiers.add(id(verifier))
        try:
            register(self._on_skill_promoted)
        except Exception:
            self._skill_memory_verifiers.discard(id(verifier))
            logger.warning(
                "[wiring] failed to bind skill verifier to memory",
                exc_info=True,
            )

    def _on_skill_promoted(self, artifact: Any) -> None:
        """Write a promoted skill into the lesson nodes of the memory graph.

        The active :class:`~encre.improve.verification.SkillArtifact` becomes
        one ``lesson`` :class:`MemoryEntry`.  Its ``source``
        (``skill:<name>@v<version>``) and ``source_refs`` (the artifact's
        ``rollback_id``) keep the memory auditable and rollback-traceable,
        while ``version`` mirrors the artifact version.

        Re-promoting the same skill is idempotent: the existing entry is kept
        rather than duplicated.  Any failure is isolated -- promotion must
        never fail because memory could not be written.

        Args:
            artifact: The promoted artifact delivered by ``on_promoted``.
        """
        if self.memory_layers is None:
            return
        try:
            name = str(getattr(artifact, "name", "") or "").strip()
            if not name:
                return
            if self._procedural_skill_exists(name):
                return
            description = str(getattr(artifact, "description", "") or "").strip()
            version = int(getattr(artifact, "version", 1) or 1)
            rollback_id = str(getattr(artifact, "rollback_id", "") or "")
            content = f"{name}: {description}".strip().strip(":") or name
            entry = _features.MemoryEntry(
                content=content,
                type=_features.MemoryType.LESSON.value,
                importance=0.8,
                confidence=0.8,
                version=version,
                source=f"skill:{name}@v{version}",
                source_refs=[rollback_id] if rollback_id else [],
                metadata={
                    "skill_name": name,
                    "description": description,
                    "skill_version": version,
                    "rollback_id": rollback_id,
                    "skill_source": str(getattr(artifact, "source", "") or ""),
                    "trigger": str(getattr(artifact, "trigger", "") or ""),
                    "run_id": str(getattr(artifact, "run_id", "") or ""),
                },
            )
            self.memory_layers.store(entry)
        except Exception:
            logger.warning(
                "[wiring] lesson-node write failed for promoted skill",
                exc_info=True,
            )

    def _procedural_skill_exists(self, name: str) -> bool:
        """Whether the graph already holds a lesson node for skill ``name``."""
        if self.memory_layers is None:
            return False
        return any(str(entry.metadata.get("skill_name", "")) == name for entry in self.memory_layers.by_type(_features.MemoryType.LESSON))

    @staticmethod
    def _event_ledger_path(session_id: str) -> Path:
        """Return the backbone ledger path for ``session_id``.

        Reuses the project data-directory convention (``get_data_dir``) and
        lands each session's append-only ledger under ``<data_dir>/events/``.
        """
        from encre.config import get_data_dir

        name = session_id or "global"
        return get_data_dir("events") / f"{name}.jsonl"

    async def _emit_pre_model_request_hook(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> Any:
        """Bridge the phase bus ``pre_model_request`` to the hook system.

        Preserves the historical ``hook_system.emit_pre_model_request``
        semantics (a listener may return a ``modified_input`` dict).

        Args:
            messages: Current session messages.
            tools: Active tool payload (may be ``None``).

        Returns:
            The hook system's result (``dict | None``).
        """
        return await self.hook_system.emit_pre_model_request(messages, tools)

    # ── Hot reload ────────────────────────────────────────────────────────

    def reload(self, new_config: EncreConfig | None = None) -> ReloadReport:
        """Re-apply a configuration to this live loop, without rebuilding it.

        The wiring mixin built every collaborator of this loop, so it is also
        the collaborator that knows how to refresh the *derived* state when the
        configuration changes underneath them -- see
        :mod:`encre.loop.hot_reload` for the full list of surfaces and, more
        importantly, for the boundary: **the backend client, the tool
        registry, the phase pipeline, the plugin registry and the memory /
        soul / profile collaborators are NOT hot-swapped.**  A turn in flight
        streams through the backend, and replacing it (or the pipeline around
        it) under a running turn would tear that stream down, so those need a
        rebuild -- ``EncreAgent.rebuild_backend()`` for the client, a fresh loop
        for the rest.

        Each surface is refreshed independently, so a fault in one -- a broken
        rule file, an unreadable skill directory -- degrades the reload to
        *partial* instead of aborting it: the failure is reported as a warning
        on the returned report and logged with its traceback.  The reload is
        also audited on this loop's append-only backbone as
        ``config_reloaded`` (and mirrored on the typed event stream as
        :class:`~encre.events.lifecycle.ConfigReloaded`), so the change of
        configuration is visible to telemetry and plugin taps.

        Idempotent: every call re-reads each surface from disk, and the
        surfaces are derived rather than accumulated, so calling this twice in
        a row leaves the same state as calling it once --
        :attr:`ReloadReport.config_changed` is ``False`` on the second call
        when the configuration is unchanged.

        Args:
            new_config: The configuration to put in force.  ``None`` re-applies
                the configuration already held, which is the "re-read my rule
                and skill files" case.

        Returns:
            The :class:`~encre.loop.hot_reload.ReloadReport`.
        """
        from encre.events.ledger import KIND_CONFIG_RELOADED
        from encre.events.lifecycle import ConfigReloaded
        from encre.loop.hot_reload import apply_config_reload

        config = self.config if new_config is None else new_config
        report = apply_config_reload(self, config)

        session_id = getattr(self.session, "id", "") or ""
        try:
            self.event_bus.publish(
                KIND_CONFIG_RELOADED,
                session_id=session_id,
                payload=report.to_payload(),
            )
        except Exception:
            logger.warning("[reload] ledger publish failed", exc_info=True)
        try:
            self.event_stream.publish(
                ConfigReloaded(
                    session_id=session_id,
                    config_fingerprint=report.config_fingerprint,
                    config_changed=report.config_changed,
                    surfaces=report.surfaces,
                    skills_reloaded=report.skills_reloaded,
                    warnings=report.warnings,
                )
            )
        except Exception:
            logger.warning("[reload] event stream publish failed", exc_info=True)
        return report
