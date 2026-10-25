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

"""Compatibility layer for OpenAI Codex CLI configuration.

Encre reads Codex's standard configuration files so users can migrate without
renaming or rewriting anything:

* ``~/.codex/config.toml`` (user-level)
* ``<workspace>/.codex/config.toml`` (project-level)
* ``~/.codex/AGENTS.md`` / ``AGENTS.override.md`` (global agent instructions)
* ``AGENTS.md`` / ``AGENTS.override.md`` discovered from the project root down
  to the current working directory (layered project instructions)

The extracted data is mapped onto Encre's existing concepts instead of
introducing parallel systems:

* Text instructions go through ``RulesLoader`` and end up in the system prompt.
* ``agents.<name>`` entries are registered as Encre sub-agents.
* ``skills.config`` items are registered as Encre skills.
* ``approval_policy`` is translated into Encre permission settings.
* ``mcp_servers`` entries are merged into ``EncreConfig.mcp_servers``.
"""

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from encre.config import SubAgentConfig
from encre.skills.registry import EncreSkillRegistry
from encre.skills.types import BundledSkillDefinition, SkillSource
from encre.tools._encoding import decode_bytes

logger = logging.getLogger(__name__)

_CODEX_HOME_ENV = "CODEX_HOME"
_DEFAULT_AGENTS_MAX_BYTES = 32768


def _get_codex_home() -> Path:
    """Return the Codex user configuration directory.

    Honors the ``CODEX_HOME`` environment variable, otherwise ``~/.codex``.
    """
    env = os.environ.get(_CODEX_HOME_ENV)
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex"


def _load_toml_nested(path: str | Path) -> dict[str, Any]:
    """Parse a TOML file and return a nested dictionary.

    Unlike ``config._load_toml`` this preserves nested tables so we can read
    ``agents.<name>`` and ``mcp_servers.<id>`` structures unchanged.
    """
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Failed to parse Codex TOML %s: %s", path, e)
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _read_text_file(path: str | Path, max_bytes: int | None = None) -> str:
    """Read a text file with an optional size cap.

    Files larger than ``max_bytes`` are truncated so a single oversized
    instruction file cannot blow up the context window.
    """
    try:
        raw = Path(path).read_bytes()
    except (OSError, UnicodeDecodeError) as e:
        logger.debug("Could not read Codex instruction file %s: %s", path, e)
        return ""
    if max_bytes is not None and len(raw) > max_bytes:
        raw = raw[:max_bytes]
    return decode_bytes(raw).strip()


@dataclass
class CodexAgentConfig:
    """One agent definition from ``agents.<name>`` in a Codex config file."""

    name: str
    description: str = ""
    config_file: str = ""
    nickname_candidates: list[str] = field(default_factory=list)


@dataclass
class CodexContext:
    """Aggregated Codex configuration relevant to an Encre session."""

    # Ordered text instruction blocks (closest to cwd last, so they take
    # precedence in the prompt).
    instructions: list[tuple[str, str]] = field(default_factory=list)
    # Codex agents translated into Encre sub-agents.
    agents: list[CodexAgentConfig] = field(default_factory=list)
    # Codex skills.config entries.
    skill_configs: list[dict[str, Any]] = field(default_factory=list)
    # Codex approval_policy value.
    approval_policy: Any = None
    # Codex mcp_servers mapping.
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    # ``project_doc_fallback_filenames`` from user config.
    fallback_filenames: list[str] = field(default_factory=list)
    # ``project_doc_max_bytes`` from user config.
    max_bytes: int = _DEFAULT_AGENTS_MAX_BYTES
    # Paths that contributed data (for UI / debugging only).
    source_paths: list[str] = field(default_factory=list)


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    return []


def _find_git_root(start: Path) -> Path | None:
    """Walk up from ``start`` looking for a ``.git`` directory."""
    current = start.resolve()
    for _ in range(256):  # hard ceiling to avoid infinite loops on odd FS
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _load_agents_layer(
    directory: Path,
    fallback_filenames: list[str],
    max_bytes: int,
) -> tuple[str, str] | None:
    """Load at most one instruction file from ``directory``.

    Priority order matches Codex: ``AGENTS.override.md``, ``AGENTS.md``,
    then any configured fallback filenames.
    """
    candidates = ["AGENTS.override.md", "AGENTS.md", *_coerce_str_list(fallback_filenames)]
    for filename in candidates:
        path = directory / filename
        if not path.is_file():
            continue
        content = _read_text_file(path, max_bytes=max_bytes)
        if content:
            return (str(path), content)
    return None


def load_agents_chain(
    workspace_path: str,
    cwd: str | None = None,
    fallback_filenames: list[str] | None = None,
    max_bytes: int = _DEFAULT_AGENTS_MAX_BYTES,
) -> list[tuple[str, str]]:
    """Return ordered ``(path, content)`` pairs for Codex AGENTS.md files.

    Loading order (later entries override earlier ones):

    1. ``~/.codex/AGENTS.override.md`` or ``~/.codex/AGENTS.md``
    2. Project root ``AGENTS.md`` (if workspace is inside a git repo)
    3. Each directory down from the project root to ``cwd``

    Each directory contributes at most one file. Empty files are skipped.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return []

    fallbacks = list(fallback_filenames or [])
    instructions: list[tuple[str, str]] = []

    # Global layer.
    global_layer = _load_agents_layer(_get_codex_home(), fallbacks, max_bytes)
    if global_layer is not None:
        instructions.append(global_layer)

    # Project layer: start at git root if we can find one, otherwise workspace.
    workspace = Path(workspace_path).resolve()
    root = _find_git_root(workspace) or workspace

    # Determine the leaf directory we should descend to.
    leaf = Path(cwd).resolve() if cwd else workspace
    if not leaf.is_dir():
        leaf = leaf.parent

    # Ensure leaf is under root; if not, just use the root.
    try:
        leaf.relative_to(root)
    except ValueError:
        leaf = root

    # Walk from root down to leaf, collecting at most one file per directory.
    current = root
    for _ in range(256):
        layer = _load_agents_layer(current, fallbacks, max_bytes)
        if layer is not None:
            instructions.append(layer)
        if current == leaf:
            break
        # Pick the next directory on the path to leaf.
        try:
            next_dir = next(
                p for p in leaf.relative_to(current).parts[:1]
            )
        except (ValueError, StopIteration):
            break
        current = current / next_dir

    return instructions


def load_user_config() -> dict[str, Any]:
    """Load ``~/.codex/config.toml`` if it exists."""
    return _load_toml_nested(_get_codex_home() / "config.toml")


def load_project_config(workspace_path: str) -> dict[str, Any]:
    """Load ``<workspace>/.codex/config.toml`` if it exists."""
    if not workspace_path:
        return {}
    return _load_toml_nested(Path(workspace_path) / ".codex" / "config.toml")


def _extract_agents(raw: dict[str, Any]) -> list[CodexAgentConfig]:
    """Extract ``agents.<name>`` blocks from a parsed Codex config."""
    agents_data = raw.get("agents")
    if not isinstance(agents_data, dict):
        return []
    agents: list[CodexAgentConfig] = []
    for name, value in agents_data.items():
        if not isinstance(value, dict):
            continue
        agents.append(
            CodexAgentConfig(
                name=str(name).strip(),
                description=str(value.get("description", "")).strip(),
                config_file=str(value.get("config_file", "")).strip(),
                nickname_candidates=_coerce_str_list(value.get("nickname_candidates")),
            )
        )
    return agents


def _extract_skill_configs(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract entries from ``skills.config`` or the legacy ``skills`` list."""
    skills_data = raw.get("skills")
    if isinstance(skills_data, dict):
        config = skills_data.get("config")
        if isinstance(config, list):
            return [dict(item) for item in config if isinstance(item, dict)]
    if isinstance(skills_data, list):
        return [dict(item) for item in skills_data if isinstance(item, dict)]
    return []


def _is_codex_internal_mcp(name: str, raw: dict[str, Any]) -> bool:
    """Whether an ``[mcp_servers.<name>]`` entry is Codex's own plumbing.

    The Codex desktop app injects helper MCP servers (its computer-use
    ``node_repl`` runtime, ...) into ``config.toml`` with commands that live
    inside the Codex installation directories.  These are not user
    configuration: they are meaningless outside a live Codex session (they
    need Codex's native pipes) and, because they are re-merged on every
    agent start, they kept re-appearing in Encre's MCP settings even after
    the user deleted them there.  Such entries are never merged.
    """
    command = str(raw.get("command", "") or "").strip()
    if not command:
        return False
    norm = command.replace("/", "\\").lower()
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        runtime_root = os.path.join(local, "OpenAI", "Codex").replace("/", "\\").lower()
        if norm.startswith(runtime_root + "\\"):
            return True
    # Belt and braces: the known helper binary, whatever install path it has.
    return os.path.basename(norm) in ("node_repl.exe", "node_repl")


def build_codex_context(
    workspace_path: str,
    cwd: str | None = None,
) -> CodexContext:
    """Aggregate Codex configuration for the active workspace."""
    ctx = CodexContext()

    user_raw = load_user_config()
    project_raw = load_project_config(workspace_path)

    # Effective config: project overrides user for shared keys.
    merged: dict[str, Any] = {}
    merged.update(user_raw)
    merged.update(project_raw)

    ctx.source_paths = []
    user_cfg_path = _get_codex_home() / "config.toml"
    project_cfg_path = Path(workspace_path) / ".codex" / "config.toml"
    if user_cfg_path.is_file():
        ctx.source_paths.append(str(user_cfg_path))
    if project_cfg_path.is_file():
        ctx.source_paths.append(str(project_cfg_path))

    # Document discovery settings (only user config is meaningful here per
    # Codex docs, but tolerate project overrides too).
    raw_fallbacks = merged.get("project_doc_fallback_filenames")
    ctx.fallback_filenames = _coerce_str_list(raw_fallbacks)

    max_bytes_raw = merged.get("project_doc_max_bytes")
    if isinstance(max_bytes_raw, int) and max_bytes_raw > 0:
        ctx.max_bytes = max_bytes_raw
    elif isinstance(max_bytes_raw, float) and max_bytes_raw > 0:
        ctx.max_bytes = int(max_bytes_raw)

    # AGENTS.md chain.
    ctx.instructions = load_agents_chain(
        workspace_path,
        cwd=cwd,
        fallback_filenames=ctx.fallback_filenames,
        max_bytes=ctx.max_bytes,
    )

    # Inline instructions from config.toml.
    dev_instructions = merged.get("developer_instructions")
    if isinstance(dev_instructions, str) and dev_instructions.strip():
        ctx.instructions.append((str(project_cfg_path or user_cfg_path), dev_instructions.strip()))

    model_instructions_file = merged.get("model_instructions_file")
    if isinstance(model_instructions_file, str) and model_instructions_file.strip():
        # Resolve relative to project config if project config exists, else user config dir.
        base = project_cfg_path.parent if project_cfg_path.is_file() else _get_codex_home()
        file_path = base / model_instructions_file
        content = _read_text_file(file_path, max_bytes=ctx.max_bytes)
        if content:
            ctx.instructions.append((str(file_path), content))

    ctx.agents = _extract_agents(merged)
    ctx.skill_configs = _extract_skill_configs(merged)
    ctx.approval_policy = merged.get("approval_policy")

    mcp_data = merged.get("mcp_servers")
    if isinstance(mcp_data, dict):
        ctx.mcp_servers = {
            str(name): raw
            for name, raw in mcp_data.items()
            if isinstance(raw, dict) and not _is_codex_internal_mcp(str(name), raw)
        }

    return ctx


def _agent_prompt_from_config_file(
    agent: CodexAgentConfig,
    workspace_path: str,
) -> str:
    """Resolve an agent's prompt from its optional ``config_file``."""
    if not agent.config_file:
        return ""
    base = Path(workspace_path).resolve()
    path = base / agent.config_file
    if not path.is_file():
        path = _get_codex_home() / agent.config_file
    if not path.is_file():
        return ""
    raw = _load_toml_nested(path)
    # Codex role layers commonly use ``developer_instructions`` or ``prompt``.
    for key in ("developer_instructions", "prompt", "system_prompt", "instructions"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def register_codex_agents_as_sub_agents(
    agent: Any,
    agents: list[CodexAgentConfig],
    workspace_path: str,
) -> int:
    """Register Codex ``agents.<name>`` entries as Encre sub-agents.

    Codex agents are conceptually delegated workers, so they map onto
    :class:`SubAgentConfig` instead of skills. The optional ``config_file``
    is parsed for a ``system_prompt``; if absent the agent's ``description``
    is used so the sub-agent still has a useful identity.
    """
    registered = 0
    existing_names = {sa.name for sa in getattr(agent.config, "sub_agents", [])}
    for codex_agent in agents:
        name = codex_agent.name.lower().replace("_", "-")
        if not name or name in existing_names:
            continue
        prompt = _agent_prompt_from_config_file(codex_agent, workspace_path)
        if not prompt:
            prompt = codex_agent.description or f"Codex agent: {codex_agent.name}"

        sub_agent = SubAgentConfig(
            name=name,
            description=codex_agent.description or name,
            system_prompt=prompt,
            hidden=False,
            tool_policy="all",
        )
        agent.config.sub_agents.append(sub_agent)
        existing_names.add(name)
        registered += 1
    return registered


def register_codex_skill_configs(
    registry: EncreSkillRegistry,
    configs: list[dict[str, Any]],
) -> int:
    """Register items from Codex ``skills.config`` as Encre skills.

    We accept the same YAML-frontmatter-style shape used by Encre skills:
    ``name``, ``description``, ``prompt``/``body`` and optional ``aliases``.
    """
    registered = 0
    for raw in configs:
        name = str(raw.get("name", "")).strip().lower().replace("_", "-")
        if not name:
            continue
        description = str(raw.get("description", "")).strip()
        body = str(raw.get("prompt") or raw.get("body") or raw.get("instructions") or "").strip()
        if not body:
            body = description or name
        aliases = _coerce_str_list(raw.get("aliases"))

        async def _get_prompt(
            args: str | None = None,
            _ctx: dict[str, Any] | None = None,
            skill_body: str = body,
        ) -> str:
            resolved = skill_body
            if args is not None:
                resolved = resolved.replace("{{args}}", args)
                resolved = resolved.replace("{{arguments}}", args)
                resolved = resolved.replace("{{user_input}}", args)
            return resolved

        skill = BundledSkillDefinition(
            name=name,
            description=description or name,
            get_prompt_for_command=_get_prompt,
            aliases=[a.lower().replace("_", "-") for a in aliases if a],
            source=SkillSource.PROJECT,
            body=body,
        )
        registry.register(skill)
        registered += 1
    return registered


def approval_policy_to_permission_settings(approval_policy: Any) -> dict[str, str]:
    """Translate a Codex ``approval_policy`` into Encre permission settings.

    Codex values:
      * ``never``        -> allow everything
      * ``on-request``   -> ask for dangerous/sensitive operations
      * ``untrusted``    -> deny by default
      * granular object  -> map enabled categories to allow, disabled to deny

    A ``__default__`` key captures the coarse policy; specific capabilities
    are also emitted when granular config is present.
    """
    settings: dict[str, str] = {}
    if approval_policy is None:
        return settings

    if isinstance(approval_policy, str):
        value = approval_policy.strip().lower()
        if value == "never":
            settings["__default__"] = "allow"
        elif value == "on-request":
            settings["__default__"] = "ask"
        elif value == "untrusted":
            settings["__default__"] = "deny"
        return settings

    if isinstance(approval_policy, dict):
        granular = approval_policy.get("granular")
        if isinstance(granular, dict):
            mapping = {
                "sandbox_approval": "sandbox",
                "rules": "rules",
                "mcp_elicitations": "mcp",
                "request_permissions": "request_permissions",
                "skill_approval": "skill",
            }
            for codex_key, enc_key in mapping.items():
                val = granular.get(codex_key)
                if isinstance(val, bool):
                    settings[enc_key] = "allow" if val else "deny"
            # If no explicit default was given, infer one from the granular map.
            if "__default__" not in settings:
                # When everything is allowed, behave like ``never``.
                if all(isinstance(granular.get(k), bool) and granular[k] for k in mapping):
                    settings["__default__"] = "allow"
                # When everything is denied, behave like ``untrusted``.
                elif all(isinstance(granular.get(k), bool) and not granular[k] for k in mapping):
                    settings["__default__"] = "deny"
                else:
                    settings["__default__"] = "ask"
        return settings

    return settings


def merge_mcp_servers(
    existing: list[dict[str, Any]],
    codex_servers: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge Codex ``mcp_servers`` into Encre's ``mcp_servers`` list format.

    Encre stores the list as ``[{name, ...}, ...]`` while Codex uses the
    standard ``{name: {...}}`` map. Existing entries take precedence over
    Codex entries with the same name.
    """
    if not isinstance(codex_servers, dict) or not codex_servers:
        return list(existing)

    merged = {str(item.get("name", "")): dict(item) for item in existing if isinstance(item, dict)}
    for name, raw in codex_servers.items():
        if not isinstance(raw, dict):
            continue
        if name in merged:
            continue
        spec = dict(raw)
        spec["name"] = name
        merged[name] = spec
    return list(merged.values())


def apply_codex_config(
    agent: Any,
    workspace_path: str,
    cwd: str | None = None,
) -> None:
    """Apply Codex configuration to a live Encre agent instance.

    This is the single entry point called from ``EncreAgent.__init__``.
    It maps Codex concepts onto Encre's existing subsystems without
    introducing duplicate state.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return

    ctx = build_codex_context(workspace_path, cwd=cwd)
    has_any = (
        ctx.instructions
        or ctx.agents
        or ctx.skill_configs
        or ctx.mcp_servers
        or ctx.approval_policy is not None
    )
    if not has_any:
        return

    # Codex agents become Encre sub-agents; skill configs become skills.
    if ctx.agents:
        try:
            register_codex_agents_as_sub_agents(agent, ctx.agents, workspace_path)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to register Codex agents as sub-agents: %s", e)

    if ctx.skill_configs:
        try:
            register_codex_skill_configs(agent.skill_registry, ctx.skill_configs)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to register Codex skill configs: %s", e)

    # Approval policy -> permission settings.
    perm_updates = approval_policy_to_permission_settings(ctx.approval_policy)
    if perm_updates:
        agent.config.permission_settings.update(perm_updates)
        if hasattr(agent, "safety") and agent.safety is not None:
            try:
                agent.safety._sync_policies_to_native()
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed to sync Codex approval policy: %s", e)

    # MCP servers -> EncreConfig.mcp_servers.
    if ctx.mcp_servers:
        try:
            merged = merge_mcp_servers(agent.config.mcp_servers, ctx.mcp_servers)
            agent.config.mcp_servers = merged
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to merge Codex MCP servers: %s", e)

    logger.debug(
        "Applied Codex config from %s: %d instruction blocks, %d agents, %d skills, %d mcp servers",
        ", ".join(ctx.source_paths) or "AGENTS.md chain",
        len(ctx.instructions),
        len(ctx.agents),
        len(ctx.skill_configs),
        len(ctx.mcp_servers),
    )
