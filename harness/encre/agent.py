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

from encre.logging_config import get_logger

"""Top-level :class:`EncreAgent` facade that wires every harness subsystem
into a single, runable object.

The harness is composed of many cooperating subsystems — a tool registry,
a hook system, a skill registry, a safety engine, an error-recovery engine,
a session, a profile/soul/memory system, a plugin host, and the
:class:`EncreLoop` that drives an actual LLM turn.  :class:`EncreAgent` is
the assembly point: it owns the configuration, constructs each subsystem
(either from the caller or from sensible defaults), and then passes them all
into :class:`EncreLoop` so that a single call to :meth:`run` produces the
stream of agent events that the front-end consumes.

The module also owns the process-wide singletons for the shared skill and
plugin registries.  Those registries are intentionally shared so that
multiple agents in the same process (e.g. server + background cron jobs) do
not re-discover the same EA packages or re-import the same plugin modules.
The singletons are lazily constructed and guarded by locks so that concurrent
agent creation never triggers a double-discovery.
"""

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from encre.goal import EncreGoalLoop
    from encre.skills.types import SkillSource
    from encre.swarm.session import EncreSwarmSession

from encre.config import EncreConfig, SubAgentConfig
from encre.hooks.file_loader import load_project_hooks
from encre.hooks.system import EncreHookSystem
from encre.improve.evolution.config import EvolutionConfig
from encre.improve.feedback.learner import EncreFeedbackLearner
from encre.loop import EncreLoop
from encre.memdir.system import EncreMemorySystem
from encre.modes.mode_profiles import AgentMode
from encre.plugins.host import PluginHost
from encre.plugins.registry import PluginRegistry
from encre.profile.system import EncreProfileSystem
from encre.recovery import ErrorRecoveryEngine
from encre.safety import EncreSafetyEngine
from encre.session import EncreSession
from encre.skills.bundled import create_bundled_skills
from encre.skills.registry import EncreSkillRegistry, parse_yaml_frontmatter
from encre.slash_commands import (
    EncreCommandRegistry,
    load_project_commands,
    load_user_commands,
)
from encre.soul.system import EncreSoulSystem
from encre.telemetry import EncreTelemetry
from encre.tools.registry import ToolRegistry
from encre.utils.types import AgentEvent, ToolCallStart, ToolProgress

# Shared registries and project-layout constants are owned by the kernel
# (``encre.kernel.services``) so the loop never has to import this surface
# module.  The historical private names are kept as aliases for callers that
# still import them from ``encre.agent``.
from encre.kernel.services import (
    PROJECT_SKILLS_CANDIDATE_DIRS as _PROJECT_SKILLS_CANDIDATE_DIRS,
)
from encre.kernel.services import (
    PROJECT_SUB_AGENT_CANDIDATE_DIRS as _PROJECT_SUB_AGENT_CANDIDATE_DIRS,
)
from encre.kernel.services import ensure_plugins_discovered
from encre.kernel.services import (
    shared_plugin_registry as _get_shared_plugin_registry,
)
from encre.kernel.services import (
    shared_plugin_registry_raw as _get_shared_plugin_registry_raw,
)
from encre.kernel.services import (
    shared_skill_registry_raw as _get_shared_skill_registry_raw,
)

logger = get_logger(__name__)

# Process-wide flags that track which discovery / load steps have already run.
# The registries they protect live in ``encre.kernel.services``; these flags
# only describe this module's own loaders.
_BUNDLED_SKILLS_LOADED = False
_USER_SKILLS_LOADED_DIRS: set[str] = set()
_PROJECT_SKILLS_LOADED_DIRS: set[str] = set()
_PROJECT_SUB_AGENT_LOADED_DIRS: set[str] = set()


def _ensure_bundled_skills_loaded(registry: EncreSkillRegistry) -> None:
    """Populate *registry* with the built-in skills shipped with the harness.

    Bundled skills come from three places: the declarative
    :func:`create_bundled_skills` factory, a local ``skills/builtin`` directory
    that documents the six inlined primitive tools (file_read/file_write/
    file_edit/bash/web_search/web_fetch — they live next to this module
    because the tools themselves are inlined into the harness rather than in
    a standalone EA package), and the ``skills/`` directory of every installed
    EA package.  The load is idempotent: the module-level flag short-circuits
    repeat calls, and the ``list_all()`` check re-runs the loaders if the
    registry was somehow emptied between calls.
    """
    global _BUNDLED_SKILLS_LOADED
    if _BUNDLED_SKILLS_LOADED and registry.list_all():
        return
    create_bundled_skills(registry)
    from encre.skills.types import SkillSource

    _builtin_skills_dir = os.path.join(os.path.dirname(__file__), "skills", "builtin")
    if os.path.isdir(_builtin_skills_dir):
        try:
            registry.load_from_dir(_builtin_skills_dir, source=SkillSource.BUNDLED)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Failed to load builtin tool skills: {e}")

    # Load skills bundled inside EA packages (ea_tool_* / ea_skill_*).  Resolves
    # via the directory scan so it works both in editable installs and inside
    # PyInstaller frozen bundles (where packages live under _internal/).
    _ensure_ea_package_skills_loaded(registry, SkillSource.BUNDLED)
    _BUNDLED_SKILLS_LOADED = True


def _ensure_ea_package_skills_loaded(registry: EncreSkillRegistry, source: SkillSource) -> None:
    """Register skills shipped in each EA package's ``skills/`` directory.

    Packages are located by the automatic directory scan in
    :mod:`encre.plugins.ea_scan` — dropping a new package into ``ea_tools/``
    is enough, no hardcoded module list.  Individual package failures are
    swallowed and logged so one bad package cannot take down every other
    bundled skill in the same process.
    """
    from encre.plugins.ea_scan import iter_ea_packages

    for pkg in iter_ea_packages():
        if pkg.skills_dir is None:
            continue
        try:
            registry.load_from_dir(str(pkg.skills_dir), source=source)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"Failed to load EA package skills from {pkg.name}: {e}")


def _ensure_user_skills_loaded(registry: EncreSkillRegistry, skills_dir: str) -> None:
    """Load user-level skills from *skills_dir*, at most once per directory.

    User skills live in the per-user data directory (typically
    ``<data_dir>/skills``) and are scoped :class:`SkillSource.USER`.  The
    ``_USER_SKILLS_LOADED_DIRS`` set is keyed by the *absolute* directory
    string so a second agent created with the same path does not re-scan the
    disk; a fresh path (different user profile) gets its own load.
    """
    global _USER_SKILLS_LOADED_DIRS
    if skills_dir in _USER_SKILLS_LOADED_DIRS:
        return
    from encre.skills.types import SkillSource

    registry.load_from_dir(skills_dir, source=SkillSource.USER)
    _USER_SKILLS_LOADED_DIRS.add(skills_dir)


def _ensure_project_skills_loaded(registry: EncreSkillRegistry, workspace_path: str) -> None:
    """Load project-level skills from the active workspace.

    Scans well-known directories (``.encre/skills`` and ``.claude/skills``)
    and reuses :meth:`EncreSkillRegistry.load_from_dir` with
    :class:`SkillSource.PROJECT` so the standard priority/override logic
    in the registry decides which copy of a same-named skill wins.  An
    empty *workspace_path* is treated as "no workspace" and short-circuits
    the whole scan, which keeps the agent usable from a bare ``python -c``
    invocation outside a project tree.
    """
    global _PROJECT_SKILLS_LOADED_DIRS
    if not workspace_path:
        return
    from encre.skills.types import SkillSource

    for rel in _PROJECT_SKILLS_CANDIDATE_DIRS:
        full = os.path.join(workspace_path, rel)
        if full in _PROJECT_SKILLS_LOADED_DIRS:
            continue
        if not os.path.isdir(full):
            continue
        try:
            registry.load_from_dir(full, source=SkillSource.PROJECT)
            _PROJECT_SKILLS_LOADED_DIRS.add(full)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to load project skills from %s: %s", full, e)


def _load_sub_agent_file(path: str) -> SubAgentConfig | None:
    """Parse a single project-level sub-agent definition file.

    Supports ``.json``, ``.yaml``/``.yml`` and ``.md`` (YAML frontmatter +
    body as ``system_prompt``).  For Markdown the frontmatter carries the
    metadata (name, description, tool_policy) and the body becomes the
    system prompt, matching the convention used by Claude Code agents.
    Returns ``None`` when the file is missing a valid ``name`` so the caller
    can skip silently — a malformed file should not prevent the rest of the
    directory from loading.
    """
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Could not read project sub-agent file %s: %s", path, e)
        return None

    data: dict[str, Any] = {}
    body = ""
    lower = path.lower()
    if lower.endswith(".json"):
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse project sub-agent JSON %s: %s", path, e)
            return None
    elif lower.endswith(".yaml") or lower.endswith(".yml"):
        try:
            data = yaml.safe_load(content) or {}
        except yaml.YAMLError as e:
            logger.warning("Failed to parse project sub-agent YAML %s: %s", path, e)
            return None
    elif lower.endswith(".md"):
        metadata, body = parse_yaml_frontmatter(content)
        data = metadata
    else:
        return None

    if not isinstance(data, dict):
        return None

    name = str(data.get("name", "")).strip().lower().replace("_", "-")
    if not name:
        return None

    system_prompt = str(data.get("system_prompt") or data.get("prompt") or "").strip()
    if not system_prompt and body:
        system_prompt = body.strip()
    description = str(data.get("description", "")).strip()
    tool_policy = str(data.get("tool_policy") or data.get("policy") or "all").strip()
    hidden = bool(data.get("hidden", False))

    return SubAgentConfig(
        name=name,
        description=description,
        system_prompt=system_prompt,
        hidden=hidden,
        tool_policy=tool_policy,
    )


def _ensure_project_sub_agents_loaded(agent: EncreAgent, workspace_path: str) -> None:
    """Load project-level sub-agents from the active workspace.

    Scans well-known directories (``.encre/agents``, ``.claude/agents`` and
    ``.codex/agents``) and merges valid definitions into
    ``agent.config.sub_agents``.  Project-level entries override builtins or
    global-config entries with the same name, matching the
    project-overrides-global convention: the caller's own files always win
    over the shipped defaults.  Files are read in sorted order so the result
    is deterministic across machines and test runs.
    """
    global _PROJECT_SUB_AGENT_LOADED_DIRS
    if not workspace_path:
        return

    for rel in _PROJECT_SUB_AGENT_CANDIDATE_DIRS:
        full = os.path.join(workspace_path, rel)
        if full in _PROJECT_SUB_AGENT_LOADED_DIRS:
            continue
        if not os.path.isdir(full):
            continue
        _PROJECT_SUB_AGENT_LOADED_DIRS.add(full)
        try:
            loaded: list[SubAgentConfig] = []
            seen: set[str] = set()
            for entry in sorted(os.listdir(full)):
                if entry.startswith("."):
                    continue
                file_path = os.path.join(full, entry)
                if not os.path.isfile(file_path):
                    continue
                sub_agent = _load_sub_agent_file(file_path)
                if sub_agent is None or sub_agent.name in seen:
                    continue
                loaded.append(sub_agent)
                seen.add(sub_agent.name)

            # Project entries override existing same-name agents.
            agent.config.sub_agents = [sa for sa in getattr(agent.config, "sub_agents", []) if sa.name not in seen]
            agent.config.sub_agents.extend(loaded)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to load project sub-agents from %s: %s", full, e)


def _get_shared_skill_registry() -> EncreSkillRegistry:
    """Return the process-wide skill registry, fully populated.

    The registry object itself is owned by :mod:`encre.kernel.services` (the
    kernel must be able to reach it without importing this surface module);
    this function adds the agent-surface loaders on top.  User skills are
    re-added on every call because they depend on :func:`get_data_dir`, which
    can change between calls when the data directory is relocated at runtime.
    """
    registry = _get_shared_skill_registry_raw()
    _ensure_bundled_skills_loaded(registry)
    from encre.config import get_data_dir

    _ensure_user_skills_loaded(registry, str(get_data_dir() / "skills"))
    return registry


class EncreAgent:
    """The top-level agent facade: configuration, subsystems, and the loop.

    Construction is cheap by default: subsystems are created from the
    caller's explicit arguments when provided, otherwise from defaults that
    follow the standard per-user data directory layout.  Plugin discovery and
    skill loading are the two expensive steps and both can be deferred with
    ``defer_plugins=True``; the actual loading happens off the event loop on
    the first call to :meth:`run` (via :meth:`ensure_plugins_loaded`).

    A single agent instance is *not* thread-safe: each caller should own its
    own :class:`EncreAgent` (or share one through an ``asyncio`` lock).  The
    shared registries live at module scope and are the only piece that is
    safe to hand out to many agents.
    """

    def __init__(
        self,
        config: EncreConfig | None = None,
        tool_registry: ToolRegistry | None = None,
        hook_system: EncreHookSystem | None = None,
        memory_system: EncreMemorySystem | None = None,
        profile_system: EncreProfileSystem | None = None,
        soul_system: EncreSoulSystem | None = None,
        skill_registry: EncreSkillRegistry | None = None,
        command_registry: EncreCommandRegistry | None = None,
        safety: EncreSafetyEngine | None = None,
        recovery: ErrorRecoveryEngine | None = None,
        plugin_registry: PluginRegistry | None = None,
        feedback: EncreFeedbackLearner | None = None,
        mode: Any = None,
        defer_plugins: bool = False,
    ) -> None:
        from encre.config import get_data_dir

        self.config = config or EncreConfig()
        self.tool_registry = tool_registry or ToolRegistry()
        self.hook_system = hook_system or EncreHookSystem()
        if memory_system is not None:
            self.memory_system = memory_system
        else:
            self.memory_system = EncreMemorySystem(str(get_data_dir() / "memory"))
        if profile_system is not None:
            self.profile_system = profile_system
        else:
            mem_dir = str(get_data_dir() / "memory")
            self.profile_system = EncreProfileSystem(mem_dir)
            self.profile_system.load()
        if soul_system is not None:
            self.soul_system = soul_system
        else:
            self.soul_system = EncreSoulSystem()
            self.soul_system.ensure_defaults()
            self.soul_system.load()
        self.safety = safety or EncreSafetyEngine(self.config, workspace=str(self.config.workspace or ""))
        self.recovery = recovery or ErrorRecoveryEngine()
        self.feedback = feedback or EncreFeedbackLearner()
        self.session = EncreSession(self.config)
        # Inject built-in sub-agents (hidden from settings UI).  These are
        # appended only when the user has not already defined a same-named
        # agent, so user config always wins over the shipped defaults.
        from encre.agents.builtin import get_builtin_sub_agents

        existing_names = {sa.name for sa in self.config.sub_agents}
        for builtin in get_builtin_sub_agents():
            if builtin.name not in existing_names:
                self.config.sub_agents.append(builtin)
        # Load project-level sub-agents for the active workspace. Project
        # entries override builtins/global-config entries with the same name.
        _ensure_project_sub_agents_loaded(self, self.config.workspace)
        self.telemetry = EncreTelemetry(
            enabled=self.config.telemetry_enabled,
            otlp_config=self.config,
        )
        self.evolution = EvolutionConfig.create_default()
        # Capability profile: one engine, distinct per-mode behaviour.
        # Defaults to GENERAL when unset so existing callers are unchanged.
        self.mode = mode or AgentMode.GENERAL
        if plugin_registry is not None:
            self.plugin_registry = plugin_registry
        elif defer_plugins:
            # Do not trigger discovery here: the caller wants construction to
            # return immediately and will await ``ensure_plugins_loaded`` later.
            self.plugin_registry = _get_shared_plugin_registry_raw()
        else:
            self.plugin_registry = _get_shared_plugin_registry()
        self.skill_registry = skill_registry or (_get_shared_skill_registry_raw() if defer_plugins else _get_shared_skill_registry())
        # Load project-level skills for the active workspace.  This is a
        # no-op when no workspace is configured or when neither
        # .encre/skills nor .claude/skills exists in the workspace root.
        _ensure_project_skills_loaded(self.skill_registry, self.config.workspace)
        # Slash commands follow the same project-discovery pattern as
        # skills: builtin commands stay registered, project-level
        # ``.encre/commands`` and ``.claude/commands`` files are layered
        # on top with priority-based merging.  User-level commands (home
        # directory + settings.json) are loaded first and persist across
        # workspace switches; project commands reload per workspace.
        self.command_registry = command_registry or EncreCommandRegistry()
        load_user_commands(self.command_registry)
        load_project_commands(self.config.workspace, self.command_registry)
        # Wire project-level hooks from ``.encre/hooks.yaml`` or
        # ``.claude/settings.json`` straight into the live hook system
        # so they fire alongside the in-process handlers.
        if self.config.workspace:
            try:
                load_project_hooks(self.hook_system, self.config.workspace)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed to load project hooks: %s", e)
            # Apply OpenAI Codex CLI configuration (AGENTS.md chain,
            # ``agents.<name>``, ``skills.config``, ``approval_policy``,
            # ``mcp_servers``) onto Encre's existing subsystems.
            try:
                from encre.codex_compat import apply_codex_config

                apply_codex_config(self, self.config.workspace)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed to apply Codex config: %s", e)
        self.loop = EncreLoop(
            self.config,
            self.session,
            self.tool_registry,
            self.hook_system,
            self.safety,
            self.memory_system,
            profile_system=self.profile_system,
            soul_system=self.soul_system,
            skill_registry=self.skill_registry,
            telemetry=self.telemetry,
            evolution=self.evolution,
            recovery=self.recovery,
            feedback=self.feedback,
            mode=self.mode,
        )
        self._wire_tools()
        self._plugin_host = PluginHost(
            tool_registry=self.tool_registry,
            hook_system=self.hook_system,
        )
        self._plugins_loaded = False
        if not defer_plugins:
            self._load_plugins()
        # MCP lifecycle (lazy init on first run): connecting to every
        # configured MCP server is expensive and depends on the plugin
        # contributions being in place, so it is deferred until the first
        # turn rather than at construction time.
        self._mcp_tools: list[Any] = []
        self._mcp_initialized = False
        # Skill-learning engine: crystallises repeated tool patterns into
        # auto-generated skills.  Previously only iClaw wired this; here we
        # make it available on the main agent path too (started lazily on
        # first run, fed after each run, stopped on close).
        from encre.improve.learning.engine import LearningEngine

        self._learning_engine = LearningEngine(self)
        self._learning_engine_started = False
        self._learning_engine_start_lock = __import__("asyncio").Lock()
        # Engine-install bridge: computer-use sessions call this when
        # the bundled chromium binary is missing; we yield the
        # resulting events to the agent's stream so the user (not the
        # LLM) sees the prompt.
        from encre.computer.engine_bridge import EngineRequester

        self._engine_requester = EngineRequester()
        self._install_engine_requester()

    @property
    def plugin_host(self) -> PluginHost:
        """The plugin host (surface accumulators for server/frontend)."""
        return self._plugin_host

    def _install_engine_requester(self) -> None:
        """Propagate the engine-install requester to all tools that
        need it (computer, browser, etc.).  Idempotent.

        We import the module-level ``set_engine_requester`` function
        from each tool module and call it -- this sets the requester
        on the singleton session so that any subsequent browser /
        desktop action routes the install prompt through the requester
        instead of returning an error to the LLM.
        """
        from encre.tools.runtime import configure_browser_engine_requester

        configure_browser_engine_requester(self._engine_requester)

    def set_engine_emit(self, emit: Any) -> None:
        """Install the immediate-emit hook on the engine requester.

        Called by the WebSocket router with a callable that
        serializes an :class:`EngineInstallRequest` and sends it
        over the wire.  After this is set, the requester will
        fire the event the moment a browser / desktop session
        asks for an install, *without* waiting for the agent's
        event loop to tick -- so the desktop dialog pops up
        promptly even when the loop is blocked on the tool call.
        """
        self._engine_requester.set_emit(emit)

    def resolve_engine_install(self, request_id: str, choice: str) -> bool:
        """Called by the WebSocket layer when the user picks an
        option from the desktop dialog.  Returns True if the
        request was found and resolved; False when the request id
        is unknown (already answered or expired).
        """
        return self._engine_requester.resolve(request_id, choice)

    async def run(
        self,
        prompt: str,
        system_prompt: str | None = None,
        custom_instructions: str = "",
    ) -> AsyncGenerator[AgentEvent, None]:
        """Execute one user turn and yield the resulting agent events.

        This is the primary entry point used by the WebSocket and CLI
        front-ends.  It ensures deferred plugin loading is complete,
        initialises MCP connections on first use, forwards every pending
        engine-install request ahead of the next tool event so the dialog
        pops up promptly, and then runs the post-run bookkeeping (profile
        inference, soul preference learning, skill crystallisation) after
        the last event has been yielded.
        """
        # Ensure deferred plugin loading has completed before the first turn,
        # so the full tool/skill set is registered without blocking construction.
        await self.ensure_plugins_loaded()
        # Lazy-init MCP connections on first run
        if not self._mcp_initialized:
            await self._init_mcp()
            self._mcp_initialized = True
        tool_names: list[str] = []
        slash_commands = [cmd.to_dict() for cmd in self.command_registry.list_all()]
        async for event in self.loop.run(
            prompt,
            system_prompt,
            custom_instructions=custom_instructions,
            slash_command_mode=self.config.slash_command_mode,
            slash_commands=slash_commands,
        ):
            if isinstance(event, ToolCallStart):
                tool_names.append(event.name)
            elif isinstance(event, ToolProgress):
                tool_names.append(event.tool_name)
            # Forward every pending engine-install request before
            # we yield any other event from the tool, so the dialog
            # can pop up promptly and the tool's await on the
            # requester future resolves cleanly.
            async for req in self._engine_requester.drain():
                yield req
            yield event
        async for req in self._engine_requester.drain():
            yield req
        # Trigger async profile inference after session completes
        if hasattr(self, "profile_system") and self.profile_system is not None:
            try:
                self._profile_inference_task = asyncio.create_task(
                    self.profile_system.infer_from_session(self.session.messages, self.loop.backend)
                )
            except Exception:
                logger.debug("exception suppressed", exc_info=True)

        # Post-run soul learning: on the normal chat path the soul USER.md
        # was never updated (only iClaw ran the post-run pipeline), so
        # "learning the user" silently died.  Extract lightweight preference
        # signals from the user's own messages and append them to USER.md,
        # rate-limited so we don't spam the file every turn.
        if getattr(self, "soul_system", None) is not None:
            with contextlib.suppress(Exception):
                self._learn_user_preferences(prompt)

        # Feed tool usage pattern to learning engine for skill crystallization
        if tool_names and self._learning_engine is not None:
            async with self._learning_engine_start_lock:
                if not self._learning_engine_started:
                    await self._learning_engine.start()
                    self._learning_engine_started = True
            with contextlib.suppress(Exception):
                await self._learning_engine.analyze_run(tool_names, prompt)

    def _learn_user_preferences(self, prompt: str) -> None:
        """Lightweight, deterministic USER.md learning for the normal chat path.

        Extracts preference signals from the current user prompt (explicit
        "I prefer/use/like/want" statements, recurring tech stack hints) and
        appends a bounded note to the soul USER.md.  Rate-limited to one
        learned note per session per distinct signal so repeated turns do not
        spam the file.  This is intentionally much cheaper than the LLM-driven
        iClaw enrichment: it runs inline after every run with zero extra API
        calls, so "understanding the user" actually advances during normal
        desktop usage.  The regex patterns are deliberately narrow — they
        only fire on explicit preference syntax, never on ambient language.
        """
        import re as _re

        soul = self.soul_system
        if soul is None or not getattr(soul, "_loaded", True):
            return
        text = (prompt or "").strip()
        if len(text) < 8 or len(text) > 2000:
            return

        signals: list[str] = []
        # Explicit preference statements.
        for _pat in (
            r"[Ii] (prefer|use|like|want|need|work with|develop in)\s+([A-Za-z0-9_+.# /-]{2,60})",
            r"my (language|stack|framework|editor|os|tool) is\s+([A-Za-z0-9_+.# /-]{2,60})",
        ):
            for _m in _re.finditer(_pat, text):
                _val = _m.group(2).strip().rstrip(".,")
                if _val and len(_val) >= 2 and _val not in signals:
                    signals.append(_val)
        if not signals:
            return

        # Rate-limit: keep a per-agent set of already-learned signals.
        _seen = getattr(self, "_learned_signals", None)
        if _seen is None:
            _seen = set()
            self._learned_signals = _seen
        _fresh = [s for s in signals if s.lower() not in _seen]
        if not _fresh:
            return
        _seen.update(s.lower() for s in _fresh)

        note = "User preferences: " + "; ".join(_fresh) + "."
        with contextlib.suppress(Exception):
            soul.append_user_note(note)

    async def run_with_tools(
        self,
        prompt: str,
        tools: list[Any],
        system_prompt: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Run a single turn with an additional set of tools registered first.

        Useful for one-shot scripting where the caller has its own tool
        objects (e.g. a custom file tool for a test harness) and does not
        want to register them permanently on :attr:`tool_registry`.  The
        tools are registered before the loop starts so the LLM can see them
        in the first payload; they remain registered for the lifetime of the
        agent afterwards.
        """
        for tool in tools:
            self.tool_registry.register(tool)
        async for event in self.run(prompt, system_prompt):
            yield event

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Append a message directly to the session transcript.

        Use this to seed the session with context the LLM should see on the
        next turn (system reminders, prior conversation history from an
        import, tool result rehydrations).  The message is added to
        :attr:`session.messages` so it participates in the next payload build
        without going through the loop's hook pipeline.
        """
        self.session.add_message(role, content, **kwargs)

    def rebuild_backend(self) -> None:
        """Recreate the loop's backend from current config.

        Call this after changing config.backend_type, config.api_key,
        config.base_url, or config.model so the backend instance matches
        the updated settings.  The active model's resolved context window
        is forwarded so compaction thresholds follow the model switch
        immediately (e.g. 1M -> 256K must trigger compaction on the next
        turn, not keep using the old window).
        """
        from encre.backend import create_backend as _cb

        active_model = self.config.get_active_model()
        thinking_config = active_model.thinking_config or self.config.thinking_config
        # apply_active_model() may have injected context_window into
        # backend_kwargs; drop it so the explicit kwarg below cannot
        # collide.  The sampling keys are spread explicitly, so a stale
        # backend_kwargs copy is dropped too.
        _sampling = self.config.active_model_sampling_overrides()
        _backend_kwargs = {
            k: v
            for k, v in self.config.backend_kwargs.items()
            if k != "context_window" and k not in _sampling
        }
        self.loop.backend = _cb(
            self.config.backend_type,
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            model=self.config.model,
            models=self.config.models,
            thinking_config=thinking_config,
            context_window=getattr(active_model, "context_window", 0) or 0,
            extra_headers=getattr(active_model, "headers", None) or None,
            # Provider "advanced config" sampling overrides; omitted keys
            # keep the backend's internal defaults.
            **_sampling,
            **_backend_kwargs,
        )

    def load_plugins(self, discover: bool = True) -> int:
        """Load and activate all plugins. Returns count of activated plugins.

        If discover=True, scans entry points and plugin directories first.

        The method is idempotent: on every call it reconciles the active
        tool set, hook chain, MCP server list, and sub-agent list against
        the current plugin registry.  Reconciliation (rather than simple
        re-add) is what allows ``reload_plugins`` to remove contributions
        from plugins that were deactivated or uninstalled since the last
        load.  Plugin tools are marked untrusted so the executor always
        wraps their output in ``<untrusted_tool_result>``.
        """
        if discover:
            # Serialise discovery so concurrent (deferred, threaded) loads run
            # it exactly once across the process.  The once-only guard is
            # owned by the kernel, which also serves the loop.
            ensure_plugins_discovered(self.plugin_registry)
        self.plugin_registry.activate_all(host=self._plugin_host.build_host_dict())

        # Replay loop-phase handlers registered before any agent existed
        # (server early-activation path): the shared registry host has no
        # hook system, so plugin ``loop.intercept`` calls made there sat in
        # limbo.  Replay is idempotent (memoised per hook system) so a
        # plugin reload cannot double-register.
        shared_host = getattr(self.plugin_registry, "host", None)
        if shared_host is not None and shared_host is not self._plugin_host:
            try:
                shared_host.replay_phase_handlers_into(self.hook_system)
            except Exception:
                logger.debug("[plugins] phase handler replay failed", exc_info=True)
        # Expose this agent's host for surfaces the prompt phase collects
        # (context providers registered through the agent path).
        with contextlib.suppress(Exception):
            self.plugin_registry.agent_host = self._plugin_host

        # Refresh the merged tool UI metadata catalog (builtins + plugin
        # contributions) so the WS transport can push it to every client.
        self._plugin_host.set_tool_meta_entries(self.plugin_registry.get_all_tool_meta())

        # Inject plugin tools.  Registration IS activation: every tool of
        # every activated plugin is registered into the active registry and
        # visible to the model from the next payload build.  There is no
        # deferred zone -- nothing waits for a manage(discover) unlock.
        # Remove tools injected by a previous load first so re-running after
        # a plugin deactivation reconciles the registry instead of leaking
        # stale tools from plugins that are no longer active.
        for stale in getattr(self, "_plugin_tool_names", set()):
            self.tool_registry.unregister(stale)
        plugin_tools = self.plugin_registry.get_all_tools()
        for tool in plugin_tools:
            self.tool_registry.register(tool)
        # Track plugin-injected tool names so a later reload can reconcile
        # the registry instead of leaking tools from deactivated plugins.
        self._plugin_tool_names = {getattr(t, "name", "") for t in plugin_tools}
        # Plugin tools execute third-party code — their output is untrusted
        # and must always go through the <untrusted_tool_result> wrapping.
        from encre.tools.executor import set_plugin_tool_names

        set_plugin_tool_names(getattr(t, "name", "") for t in self.plugin_registry.get_all_tools())

        # Inject plugin skills
        for skill in self.plugin_registry.get_all_skills():
            self.skill_registry.register(skill)

        # Inject plugin hooks.  Descriptors carry the handler plus
        # registration metadata (priority / timeout / source).  Each
        # descriptor gets a stable handler id derived from
        # (source, handler_ref) so re-running after a plugin reload
        # reconciles the chain — stale ids are removed before
        # re-registration instead of accumulating duplicates.
        stale_hook_ids: set[str] = getattr(self, "_plugin_hook_ids", set())
        for hid in stale_hook_ids:
            self.hook_system.unregister_handler(hid)
        new_hook_ids: set[str] = set()
        for event_type, descriptors in self.plugin_registry.get_all_hooks().items():
            for desc in descriptors:
                handler = desc.get("handler") if isinstance(desc, dict) else desc
                if handler is None:
                    continue
                meta = desc if isinstance(desc, dict) else {}
                source = str(meta.get("source", "?"))
                ref = str(meta.get("handler_ref", "") or getattr(handler, "__qualname__", ""))
                hid = f"plugin:{source}:{event_type}:{ref}"
                metadata = {k: v for k, v in meta.items() if k not in ("handler",) and v is not None}
                self.hook_system.register_handler(event_type, handler, handler_id=hid, metadata=metadata)
                new_hook_ids.add(hid)
        self._plugin_hook_ids = new_hook_ids

        # Inject plugin backends (previously broken `pass`): plugins that
        # ship a backend adapter register it under its manifest name so the
        # user can select it via ``backend_type`` in the settings UI.
        for name, backend_cls in self.plugin_registry.get_all_backends().items():
            try:
                from encre.backend import register_backend

                register_backend(name, backend_cls)
            except Exception:
                logger.debug("Failed to register plugin backend %s", name, exc_info=True)

        # Inject plugin-provided MCP servers (manifest ``provides_mcp``).
        # Entries are merged into ``config.mcp_servers`` and reconciled on
        # every reload so deactivating a plugin removes its servers; the
        # caller triggers ``reconnect_mcp()`` when the set changes at runtime.
        plugin_mcp = self.plugin_registry.get_all_mcp_servers()
        plugin_mcp_names = {str(e.get("name", "")) for e in plugin_mcp}
        self.config.mcp_servers = [
            s for s in self.config.mcp_servers if str(s.get("name", "")) not in getattr(self, "_plugin_mcp_names", set())
        ] + plugin_mcp
        self._plugin_mcp_names = plugin_mcp_names

        # Consume host registries (things registered via context in on_activate)
        host = self._plugin_host
        # Plugin-registered slash commands
        for name, cmd_def in host.commands.items():
            try:
                self.command_registry.register(cmd_def)
            except Exception:
                logger.debug("Failed to register plugin command %s", name, exc_info=True)
        # Plugin-provided sub-agents: declarative ``provides_agent`` entries
        # plus context-registered ``host.sub_agents``, converted to
        # SubAgentConfig and reconciled like MCP servers so a deactivated
        # plugin's agents disappear.  They never touch the main agent's
        # identity, system prompt or tool set.
        from encre.config import SubAgentConfig as _SubAgentConfig

        def _to_sub_agent(obj: Any) -> Any:
            if isinstance(obj, _SubAgentConfig) or not isinstance(obj, dict):
                return obj
            try:
                return _SubAgentConfig.from_dict(obj)
            except Exception:
                return None

        plugin_agents: list[Any] = [a for a in (_to_sub_agent(d) for d in self.plugin_registry.get_all_sub_agent_defs()) if a is not None]
        for _name, definition in host.sub_agents.items():
            converted = _to_sub_agent(definition)
            if converted is not None:
                plugin_agents.append(converted)
        plugin_agent_names = {getattr(a, "name", "") for a in plugin_agents}
        self.config.sub_agents = [
            a for a in self.config.sub_agents if getattr(a, "name", "") not in getattr(self, "_plugin_agent_names", set())
        ] + plugin_agents
        self._plugin_agent_names = plugin_agent_names

        # Fire the loop lifecycle event for lazy plugins.  Non-lazy plugins
        # are now activated and their contributions injected; plugins
        # declaring ``activation_events`` (e.g. ``session_start``) are
        # imported + activated here, and the loop's bridge syncs their
        # tools into the registry immediately.
        loop = getattr(self, "loop", None)
        if loop is not None and hasattr(loop, "_emit_plugin_event"):
            loop._emit_plugin_event("session_start")

        return self.plugin_registry.active_count

    def reload_plugins(self) -> int:
        """Re-sync plugin contributions after an activation change.

        Call after a plugin is deactivated or uninstalled so the agent's
        tool registry stops exposing tools from plugins that are no longer
        active.  ``load_plugins`` is idempotent: it first unregisters the
        tools injected by the previous pass, then re-registers the current
        active set.  Discovery is skipped (``discover=False``) because the
        plugin set has not changed on disk — only the activation flags.
        """
        self._plugins_loaded = True
        return self.load_plugins(discover=False)

    def _load_plugins(self) -> None:
        """Auto-load plugins during agent initialization."""
        if getattr(self, "_plugins_loaded", False):
            return
        self.load_plugins(discover=True)
        self._plugins_loaded = True

    async def aload_plugins(self, discover: bool = True) -> int:
        """Load and activate plugins without blocking the event loop.

        The synchronous work (module imports, activation, tool/skill/hook
        injection) is executed in a worker thread so the event loop stays
        responsive to incoming WebSocket messages while discovery runs.
        """
        return await asyncio.to_thread(self.load_plugins, discover)

    async def ensure_plugins_loaded(self) -> None:
        """Ensure plugins (and bundled skills) are loaded once, off the loop.

        Safe to call from any async entry point.  Concurrent callers are
        serialised by an ``asyncio.Lock``; once loading has completed every
        later call returns immediately.  This is what allows an agent created
        with ``defer_plugins=True`` to stay cheap to construct while still
        having the full tool set available before the first run.

        The shared skill registry is populated in the same worker thread so
        that deferred construction never pays the (disk + YAML) cost on the
        event loop.  A second check inside the lock handles the race where
        two callers both see ``_plugins_loaded == False`` before either
        acquires the lock.
        """
        if getattr(self, "_plugins_loaded", False):
            return
        lock = getattr(self, "_plugins_async_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._plugins_async_lock = lock
        async with lock:
            if getattr(self, "_plugins_loaded", False):
                return
            await self.aload_plugins(discover=True)
            await asyncio.to_thread(_get_shared_skill_registry)
            self._plugins_loaded = True

    def _wire_tools(self) -> None:
        """Wire parent loop reference to tools that need it."""
        from encre.tools.builtin import register_builtin_tools
        from encre.tools.runtime import set_parent_loop

        # Hardcoded core set (file/shell/web primitives + manage + memory)
        # is part of the harness itself, not a plugin — register it
        # unconditionally so the agent is always usable even when no
        # plugins are installed.
        register_builtin_tools(self.tool_registry)
        set_parent_loop(self.loop)

    # ------------------------------------------------------------------
    # MCP lifecycle
    # ------------------------------------------------------------------

    async def _init_mcp(self) -> None:
        """Connect configured MCP servers and register their tools.

        MCP servers are declared in ``config.mcp_servers`` with either a
        ``command`` (stdio transport) or a ``url`` (HTTP transport).  Both
        the legacy and current field names are accepted so older config files
        keep working.  A model may also open additional HTTP servers at
        runtime via ``execute(server_url=...)``; those are restricted to the
        configured URL allowlist (SSRF defense).  Servers with no command
        and no URL are silently skipped.
        """
        from encre.tools.mcp import EncreMCPTool

        # Allowlist of configured MCP HTTP URLs a model may connect to via
        # execute(server_url=...); anything else is refused (SSRF defense).
        configured_urls = [str(server.get("url") or server.get("server_url", "")).rstrip("/") for server in self.config.mcp_servers]
        configured_urls = [u for u in configured_urls if u]

        for server in self.config.mcp_servers:
            # Support both old (enabled) and new (disabled) field names
            enabled_old = server.get("enabled", True)
            disabled_new = server.get("disabled", False)
            if isinstance(enabled_old, bool):
                if not enabled_old:
                    continue
            elif disabled_new:
                continue

            # Support both old (transport) and new (type) field names
            transport = server.get("type") or server.get("transport", "stdio")
            command = server.get("command", "")
            args = server.get("args", [])
            # Support both old (server_url) and new (url) field names
            server_url = server.get("url") or server.get("server_url", "")
            env = server.get("env")
            cwd = server.get("cwd")
            # Support both old (http_timeout) and new (timeout) field names
            http_timeout = server.get("timeout") or server.get("http_timeout", 60.0)

            # Skip if no command for stdio or no URL for http
            if transport == "http":
                if not server_url:
                    continue
            elif not command:
                continue

            # Build full command as a list to preserve argument boundaries
            # (avoids fragile string re-tokenisation, especially on Windows
            # where the shell would otherwise re-split quoted arguments).
            full_command = [command, *(str(a) for a in args)] if args else [command]

            try:
                mcp_tool = EncreMCPTool(
                    command=full_command if transport == "stdio" else "",
                    server_url=server_url if transport == "http" else "",
                    env=env if env else None,
                    cwd=cwd or None,
                    http_timeout=float(http_timeout) if http_timeout else 60.0,
                    allowed_server_urls=configured_urls,
                )
                await mcp_tool.register_with(self.tool_registry, prefix="mcp__")
                self._mcp_tools.append(mcp_tool)
            except Exception:
                import logging

                logging.getLogger("encre.agent").exception("Failed to connect MCP server: %s", server.get("name", command))

    async def _disconnect_mcp(self) -> None:
        """Disconnect all MCP servers and remove their tools from the registry."""
        # Remove discovered MCP tool entries from the registry.  The key
        # prefix ``mcp__`` is the stable contract used by
        # ``register_with(prefix="mcp__")`` above.
        mcp_keys = [k for k in self.tool_registry.list_tools() if k.startswith("mcp__")]
        for key in mcp_keys:
            self.tool_registry._tools.pop(key, None)

        # Disconnect MCP clients
        for mcp in self._mcp_tools:
            with contextlib.suppress(Exception):
                await mcp._disconnect()
        self._mcp_tools.clear()

    async def reconnect_mcp(self) -> None:
        """Disconnect old MCP connections and reconnect with current config.

        Called after a plugin that contributes MCP servers is activated or
        deactivated (the plugin host reconciles ``config.mcp_servers`` and
        then triggers this so the live connections match the new set).
        """
        await self._disconnect_mcp()
        await self._init_mcp()

    def set_scheduler(self, scheduler: Any) -> None:
        """Wire a scheduler instance to the cron tools."""
        from encre.tools.runtime import set_scheduler as _set_scheduler

        _set_scheduler(scheduler)

    def reset(self) -> None:
        """Reset the agent to a fresh state, rebuilding the loop.

        Session messages, telemetry, evolution state, and MCP initialisation
        flags are all cleared; the tool registry and subsystems are reused
        so any externally-supplied objects keep their identity.  The plugin
        registry is re-activated from scratch because the new loop needs a
        clean set of plugin contributions.
        """
        self.session = EncreSession(self.config)
        self.telemetry.reset()
        self.evolution = EvolutionConfig.create_default()
        self.loop = EncreLoop(
            self.config,
            self.session,
            self.tool_registry,
            self.hook_system,
            self.safety,
            self.memory_system,
            profile_system=self.profile_system,
            soul_system=self.soul_system,
            skill_registry=self.skill_registry,
            telemetry=self.telemetry,
            evolution=self.evolution,
            recovery=self.recovery,
            feedback=self.feedback,
            mode=self.mode,
        )
        self._wire_tools()
        self._plugins_loaded = False
        self._load_plugins()
        self._mcp_initialized = False

    async def aclose(self) -> None:
        """Release all resources held by this agent.

        Closes the backend (httpx clients, model memory), clears session state,
        flushes telemetry, and disconnects MCP servers.  Every step is wrapped
        in ``contextlib.suppress`` because a failing teardown should not
        prevent the rest of the cleanup from running — a broken MCP server
        for example should not stop telemetry from flushing.
        """
        # Disconnect MCP servers
        with contextlib.suppress(Exception):
            await self._disconnect_mcp()

        # Close the agent loop (which closes the backend)
        with contextlib.suppress(Exception):
            await self.loop.aclose()

        # Stop the learning engine: cancel any in-flight crystallisation
        # tasks so a shut-down agent does not leak background skill
        # generation.
        _le = getattr(self, "_learning_engine", None)
        if _le is not None:
            with contextlib.suppress(Exception):
                await _le.stop()

        # Flush telemetry
        if self.telemetry is not None:
            with contextlib.suppress(Exception):
                self.telemetry.flush()

        # Clear session
        if self.session is not None:
            self.session.messages.clear()
            self.session.rebuild_runtime_caches()

    def activate_skill(self, name: str, args: str | None = None) -> str:
        """Activate a skill by name and return its rendered output.

        Used by the slash-command layer and by explicit ``/skill <name>``
        invocations.  The skill registry resolves the name against the
        full priority chain (project > user > bundled) and renders the
        resulting prompt with the optional ``args`` substituted.
        """
        return self.skill_registry.activate(name, args)

    def respond_permission(self, decision: bool, remember: bool = False) -> None:
        """Approve or deny the pending permission request from the current turn.

        ``remember=True`` persists an approval into the session policy table
        so the same tool is not asked again during this session.  The loop
        resolves the pending future; this method is the bridge between the
        UI (which renders the approval dialog) and the loop's await point.
        """
        self.loop.resolve_permission(decision, remember)

    def goal(
        self,
        description: str,
        success_criteria: str,
        max_attempts: int = 20,
        timeout_seconds: int = 3600,
    ) -> EncreGoalLoop:
        """Create a goal-driven autonomous loop for this agent.

        Usage:
            result = await agent.goal(
                "Implement login", "JWT tokens work, tests pass"
            ).execute()

        The goal loop repeatedly runs the agent against the same prompt
        until the success criteria are met, a max attempt count is hit,
        or the timeout expires.  It is a thin wrapper around the normal
        :meth:`run` path with a retry-and-check loop on top.
        """
        from encre.goal import EncreGoalLoop

        return EncreGoalLoop(
            self,
            description=description,
            success_criteria=success_criteria,
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )

    def swarm(
        self,
        goal: str,
        max_concurrent: int = 5,
        enable_reviewer: bool = True,
        timeout_seconds: float = 3600.0,
    ) -> EncreSwarmSession:
        """Create a multi-agent swarm session for this agent.

        Decomposes the goal, assigns roles, and executes in parallel with
        shared blackboard and optional reviewer gates.

        Usage:
            result = await agent.swarm(
                "Build a full-stack TODO app with auth"
            ).execute()

        The swarm forks child agents from this one (sharing config,
        registry, and subsystems) so each worker sees the same tools and
        skills.  ``enable_reviewer`` gates each worker's output through a
        critic sub-agent before merging back to the blackboard.
        """
        from encre.swarm.session import EncreSwarmSession

        return EncreSwarmSession(
            self,
            goal=goal,
            max_concurrent=max_concurrent,
            enable_reviewer=enable_reviewer,
            timeout_seconds=timeout_seconds,
        )
