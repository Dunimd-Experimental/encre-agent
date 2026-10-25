#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
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

"""Builtin manage tool -- the runtime plugin manager.

Lets the model dynamically install new tools, sub-agents, skills, and MCP
servers at runtime, and search the tool index for what already exists.
Discovery is purely informational: every tool of every activated plugin is
registered and visible to the model from startup (registration IS
activation -- there is no deferred/unlock zone), so ``discover`` only
reports names, categories, and parameter cards. All lifecycle changes take
effect immediately and are available on the very next turn.

Hardcoded builtin: previously shipped as the ``ea-tool-manage`` EA plugin.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any

from encre.logging_config import get_logger
from encre.plugins.types import EncrePlugin
from encre.tools.base import build_tool
from encre.tools.runtime import _resolve_loop

logger = get_logger("encre.tools.builtin.manage")


async def _manage_execute(**kwargs: Any) -> str:
    action = (kwargs.get("action") or "").strip()
    if not action:
        return (
            "Error: 'action' is required. "
            "Choose: discover, install_tool, install_agent, install_skill, "
            "install_mcp, plugin_list, plugin_install, plugin_activate, "
            "plugin_uninstall, plugin_enable, plugin_disable, plugin_reload, "
            "plugin_restart"
        )

    loop = _resolve_loop()
    if loop is None:
        return "Error: manage requires a parent loop reference."

    if action == "discover":
        return await _discover(loop, kwargs)
    elif action == "install_tool":
        return await _install_tool(loop, kwargs)
    elif action == "install_agent":
        return await _install_agent(loop, kwargs)
    elif action == "install_skill":
        return await _install_skill(loop, kwargs)
    elif action == "install_mcp":
        return await _install_mcp(loop, kwargs)
    elif action == "plugin_list":
        return await _plugin_list(loop, kwargs)
    elif action == "plugin_install":
        return await _plugin_install(loop, kwargs)
    elif action == "plugin_activate":
        return await _plugin_activate(loop, kwargs)
    elif action == "plugin_uninstall":
        return await _plugin_uninstall(loop, kwargs)
    elif action == "plugin_enable":
        return await _plugin_enable(loop, kwargs)
    elif action == "plugin_disable":
        return await _plugin_disable(loop, kwargs)
    elif action == "plugin_reload":
        return await _plugin_reload(loop, kwargs)
    elif action == "plugin_restart":
        return await _plugin_restart(loop, kwargs)
    else:
        return (
            f"Error: unknown action '{action}'. "
            "Choose: discover, install_tool, install_agent, install_skill, "
            "install_mcp, plugin_list, plugin_install, plugin_activate, "
            "plugin_uninstall, plugin_enable, plugin_disable, plugin_reload, "
            "plugin_restart"
        )


def _plugin_registry() -> Any:
    """Return the shared plugin registry (lazy import avoids cycles)."""
    try:
        from encre.agent import _get_shared_plugin_registry

        return _get_shared_plugin_registry()
    except Exception as e:
        logger.warning("plugin registry unavailable: %s", e)
        return None


def _resolve_plugin_name(registry: Any, name: str) -> str | None:
    """Resolve a manage-tool name to a real plugin name.

    Accepts the exact plugin identity first (``ea-tool-browser``).  Falls
    back to treating the name as a **tool** name and resolving the plugin
    that owns it -- first via the live ownership map (activated plugins),
    then via a scan of every registered manifest's ``provides_tools``
    (covers dormant / deactivated plugins whose tools are not in the live
    map).  Models routinely pass a tool name such as ``browser`` where the
    plugin identity ``ea-tool-browser`` is expected; this keeps such calls
    working instead of erroring.
    """
    if registry.get_manifest(name) is not None:
        return name
    owner = registry.tool_owner(name)
    if owner and registry.get_manifest(owner) is not None:
        return owner
    states = registry.snapshot_states()
    names = states.keys() if isinstance(states, dict) else []
    for plugin_name in names:
        manifest = registry.get_manifest(str(plugin_name))
        if manifest is not None and name in (getattr(manifest, "provides_tools", None) or []):
            return str(plugin_name)
    return None


def _record_manage_op(loop: Any, action: str, **fields: Any) -> None:
    """Append a lifecycle operation to the session's manage-op journal.

    The journal (``metadata["manage_ops"]``) is the rollback substrate: every
    conversation-rollback path (branch rollback, commit checkout, message
    edit/delete) reverts the journal suffix recorded after the rollback point
    by applying ``revert_manage_op`` in reverse order.  Failures to record are
    swallowed -- a missing journal entry degrades rollback fidelity but must
    never break the tool call itself.
    """
    try:
        session = getattr(loop, "session", None)
        metadata = getattr(session, "metadata", None)
        if not isinstance(metadata, dict):
            return
        ops = metadata.setdefault("manage_ops", [])
        if not isinstance(ops, list):
            return
        ops.append(
            {
                "ts": time.time(),
                # 1-based turn numbering (``EncreSession.active_turn``): the
                # rollback boundary is the restored ``turn_count`` and ops are
                # journalled mid-turn, so the raw 0-based counter tagged the
                # discarded turn one index early and it escaped the revert.
                "turn": int(getattr(session, "active_turn", 0) or 0),
                "action": action,
                **fields,
            }
        )
    except Exception as e:
        logger.warning("failed to record manage op '%s': %s", action, e)


def revert_manage_op(op: dict[str, Any]) -> bool:
    """Invert one recorded manage-tool lifecycle operation (rollback support).

    Args:
        op: A journal entry as written by ``_record_manage_op``.

    Returns:
        ``True`` when the inverse operation was applied successfully.
    """
    action = str(op.get("action", ""))
    name = str(op.get("name", "")).strip()
    registry = _plugin_registry()
    if not name or registry is None:
        return False
    if action in ("install_tool", "install_agent", "install_skill", "install_mcp"):
        # Inverse of a dynamic install: unregister the plugin shell.
        # unregister() deactivates first, which tears down the contributed
        # tool / agent / skill through the plugin's own hooks.
        return bool(registry.unregister(str(op.get("plugin_name") or name)))
    if action == "plugin_install":
        # Inverse of confirming a pending plugin: it was registered but not
        # activated before the confirmation, so deactivate only.
        return bool(registry.deactivate(name))
    if action == "plugin_activate":
        # Inverse of a logical activation: back to DISCOVERED, contributions
        # unwound through the plugin's own deactivate hooks.
        return bool(registry.deactivate(name))
    if action == "plugin_uninstall":
        # Inverse of an uninstall: restore the ledger record, re-discover the
        # package from its on-disk ``pkg_dir`` (uninstall never deletes
        # files), and re-activate.  The ledger record was captured before the
        # uninstall dropped it.
        rec = op.get("ledger_record")
        if isinstance(rec, dict) and rec:
            registry.record_installed(
                name,
                str(rec.get("version", "")),
                source=str(rec.get("source", "user")),
                pkg_dir=str(rec.get("pkg_dir", "")),
                sha256=str(rec.get("sha256", "")),
            )
            registry.discover_all()
        return bool(registry.activate(name, host=registry.default_host_dict()))
    if action == "plugin_enable":
        if op.get("had_ledger"):
            return bool(registry.set_plugin_enabled(name, False))
        # The enabled plugin had no ledger entry: the enable was a
        # session-only activation, so a session-only deactivation inverts it.
        return bool(registry.deactivate(name))
    if action == "plugin_disable":
        if op.get("had_ledger"):
            return bool(registry.set_plugin_enabled(name, True))
        return bool(registry.activate(name, host=registry.default_host_dict()))
    return False


async def _plugin_list(_loop: Any, _kwargs: dict[str, Any]) -> str:
    """List every plugin with lifecycle state, failures, and pending items."""
    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    payload: dict[str, Any] = {
        "plugins": registry.snapshot_states(),
        "failed": registry.list_failed(),
        "pending": registry.pending_plugins(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _plugin_install(_loop: Any, kwargs: dict[str, Any]) -> str:
    """Confirm and activate a PENDING third-party plugin (executes its code)."""
    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    name = (kwargs.get("name") or "").strip()
    if not name:
        pending = registry.pending_plugins()
        if pending:
            return json.dumps(
                {
                    "error": "'name' is required.",
                    "pending_plugins": pending,
                },
                ensure_ascii=False,
                indent=2,
            )
        return "Error: 'name' is required. No pending plugins await confirmation."
    ok = registry.confirm_activation(name, host=registry.default_host_dict())
    if not ok:
        reason = registry.list_failed().get(name, "activation failed")
        return json.dumps({"status": "failed", "name": name, "reason": reason}, ensure_ascii=False, indent=2)
    manifest = registry.get_manifest(name)
    _record_manage_op(_loop, "plugin_install", name=name)
    return json.dumps(
        {
            "status": "activated",
            "name": name,
            "version": getattr(manifest, "version", ""),
            "note": "The plugin's tools are available from your next turn.",
        },
        ensure_ascii=False,
        indent=2,
    )


async def _plugin_uninstall(_loop: Any, kwargs: dict[str, Any]) -> str:
    """Deactivate, unregister, and ledger-remove a plugin."""
    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    name = (kwargs.get("name") or "").strip()
    if not name:
        return "Error: 'name' is required."
    resolved = _resolve_plugin_name(registry, name)
    if resolved is None:
        return f"Error: no plugin named '{name}' is registered."
    name = resolved
    # Capture the ledger record BEFORE it is dropped so a rollback can
    # restore the plugin from it (pkg_dir, version, sha256, ...).
    ledger_record = registry.installed_records().get(name)
    deactivated = registry.deactivate(name)
    unregistered = registry.unregister(name)
    registry.record_uninstalled(name)
    _record_manage_op(_loop, "plugin_uninstall", name=name, ledger_record=ledger_record)
    return json.dumps(
        {
            "status": "uninstalled" if unregistered else "not_registered",
            "name": name,
            "deactivated": deactivated,
            "unregistered": unregistered,
        },
        ensure_ascii=False,
        indent=2,
    )


async def _plugin_enable(_loop: Any, kwargs: dict[str, Any]) -> str:
    """Re-enable a disabled plugin (ledger flag + live re-activation)."""
    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    name = (kwargs.get("name") or "").strip()
    if not name:
        return "Error: 'name' is required."
    resolved = _resolve_plugin_name(registry, name)
    if resolved is None:
        return f"Error: no plugin named '{name}' is registered."
    name = resolved
    if registry.set_plugin_enabled(name, True):
        _record_manage_op(_loop, "plugin_enable", name=name, had_ledger=name in registry.installed_records())
        state = registry.plugin_state(name)
        return json.dumps(
            {
                "status": "enabled",
                "name": name,
                "state": state.value if state else "unknown",
            },
            ensure_ascii=False,
            indent=2,
        )
    # Never user-installed: no ledger entry -- activate it directly.
    ok = registry.activate(name, host=registry.default_host_dict())
    return json.dumps(
        {
            "status": "enabled" if ok else "failed",
            "name": name,
            "note": None if ok else "activation failed; see plugin_list for the reason",
        },
        ensure_ascii=False,
        indent=2,
    )


async def _plugin_disable(_loop: Any, kwargs: dict[str, Any]) -> str:
    """Disable a plugin (ledger flag + immediate deactivation)."""
    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    name = (kwargs.get("name") or "").strip()
    if not name:
        return "Error: 'name' is required."
    resolved = _resolve_plugin_name(registry, name)
    if resolved is None:
        return f"Error: no plugin named '{name}' is registered."
    name = resolved
    if registry.set_plugin_enabled(name, False):
        _record_manage_op(_loop, "plugin_disable", name=name, had_ledger=name in registry.installed_records())
        return json.dumps({"status": "disabled", "name": name}, ensure_ascii=False, indent=2)
    # Never user-installed: no ledger entry -- deactivate for this session.
    ok = registry.deactivate(name)
    return json.dumps(
        {
            "status": "disabled" if ok else "failed",
            "name": name,
            "note": "session-only (plugin was not user-installed)" if ok else None,
        },
        ensure_ascii=False,
        indent=2,
    )


async def _plugin_reload(_loop: Any, kwargs: dict[str, Any]) -> str:
    """Hot-reload one plugin: deactivate, re-import, re-activate."""
    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    name = (kwargs.get("name") or "").strip()
    if not name:
        return "Error: 'name' is required."
    resolved = _resolve_plugin_name(registry, name)
    if resolved is None:
        return f"Error: no plugin named '{name}' is registered."
    name = resolved
    ok = registry.reload_plugin(name)
    if not ok:
        reason = registry.list_failed().get(name, "reload failed")
        return json.dumps({"status": "failed", "name": name, "reason": reason}, ensure_ascii=False, indent=2)
    return json.dumps({"status": "reloaded", "name": name}, ensure_ascii=False, indent=2)


async def _plugin_restart(_loop: Any, _kwargs: dict[str, Any]) -> str:
    """Restart the whole plugin system: deactivate all, rediscover, reactivate."""
    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    registry.deactivate_all()
    found = registry.discover_all()
    results = registry.activate_all(host=registry.default_host_dict())
    ok = sum(1 for v in results.values() if v)
    return json.dumps(
        {
            "status": "restarted",
            "rediscovered": found,
            "activated": f"{ok}/{len(results)}",
            "failed": registry.list_failed(),
        },
        ensure_ascii=False,
        indent=2,
    )


async def _discover(loop: Any, kwargs: dict[str, Any]) -> str:
    """Search the tool index and report matches.

    Pure information lookup -- it tells the model WHAT exists (name,
    category, description, parameter card). Activation is never involved:
    every registered tool is already active and callable, so a match can be
    invoked directly using the parameters from its card.
    """
    query = (kwargs.get("query") or "").strip()
    if not query:
        return "Error: 'query' is required. Describe the capability you need in natural language."

    top_k = kwargs.get("top_k", 5)
    try:
        top_k = max(1, min(int(top_k), 15))
    except (TypeError, ValueError):
        top_k = 5

    category = (kwargs.get("category") or "").strip() or None

    discovery = getattr(loop, "discovery", None)
    if discovery is None:
        return "Error: tool discovery is not initialised for this loop."

    results = discovery.search(query, top_k=top_k, category=category)
    if not results:
        cats = discovery.list_by_category()
        cat_summary = ", ".join(f"{k} ({len(v)})" for k, v in sorted(cats.items()))
        return f"No tools matched '{query}'. Try a broader query or pick a category to browse. Available categories: {cat_summary}."

    cards = []
    for r in results:
        cards.append(
            {
                "name": r["name"],
                "category": r["category"],
                "description": r["description"],
                "parameters": r["parameters"],
            }
        )

    payload = {
        "matches": cards,
        "note": ("Every match is already active and callable: invoke it directly with the arguments from its card."),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def _plugin_activate(_loop: Any, kwargs: dict[str, Any]) -> str:
    """Activate a registered (not yet active) plugin.

    Logical activation, not a physical install: flips the plugin into the
    ACTIVATED state, merging its contributions (tools / sub-agents / MCP
    servers / skills) into the live runtime.  Registered tools become
    visible to the model immediately (registration IS activation).
    """
    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    name = (kwargs.get("name") or "").strip()
    if not name:
        return "Error: 'name' is required."
    resolved = _resolve_plugin_name(registry, name)
    if resolved is None:
        return f"Error: no plugin named '{name}' is registered."
    name = resolved
    pre_state = registry.plugin_state(name)
    manifest = registry.get_manifest(name)
    if registry.activate(name, host=registry.default_host_dict()):
        # Journal the state flip (unless the plugin was already live, in which
        # case this changed nothing): a rollback that leaves the plugin
        # activated would hand the model tools the discarded turn never had.
        if getattr(pre_state, "name", "") != "ACTIVATED":
            _record_manage_op(_loop, "plugin_activate", name=name)
        return json.dumps(
            {
                "status": "activated",
                "name": name,
                "tools": getattr(manifest, "provides_tools", []) or [],
                "note": "Contributions are live and tool schemas are in your tool list.",
            },
            ensure_ascii=False,
            indent=2,
        )
    reason = registry.list_failed().get(name, "activation failed")
    return json.dumps({"status": "failed", "name": name, "reason": reason}, ensure_ascii=False, indent=2)


class _DynamicPlugin(EncrePlugin):
    """In-memory plugin shell wrapping a model-installed capability.

    Every dynamic install gets a real plugin identity: it shows up in
    ``plugin_list``, is recorded in the installed.json ledger, and can be
    disabled / uninstalled / hot-reloaded like any other plugin. Runtime
    contributions performed in ``on_activate`` go through the context's
    undo-tracked surfaces so ``deactivate`` rolls them back automatically;
    contributions without a host surface (skills, MCP connections) are
    cleaned up in ``on_deactivate`` via the provided hooks.
    """

    def __init__(
        self,
        manifest: Any,
        *,
        on_activate_fn: Any = None,
        on_deactivate_fn: Any = None,
    ) -> None:
        self.manifest = manifest
        self._on_activate_fn = on_activate_fn
        self._on_deactivate_fn = on_deactivate_fn

    def on_activate(self, context: Any = None) -> None:
        if self._on_activate_fn is not None:
            self._on_activate_fn(context)

    def on_deactivate(self) -> None:
        if self._on_deactivate_fn is not None:
            self._on_deactivate_fn()


def _make_dynamic_plugin(
    kind: str,
    name: str,
    description: str,
    *,
    provides_tools: list[str] | None = None,
    provides_skills: list[str] | None = None,
    provides_mcp: list[dict[str, Any]] | None = None,
    provides_agent: list[dict[str, Any]] | None = None,
    on_activate_fn: Any = None,
    on_deactivate_fn: Any = None,
) -> _DynamicPlugin:
    """Build a namespaced in-memory plugin for a model-installed capability."""
    from encre.plugins.types import PluginManifest, PluginSource

    manifest = PluginManifest(
        name=f"model-{kind}-{name}",
        version="1.0.0",
        description=description,
        icon="",
        author="model-runtime",
        license="Apache-2.0",
        tags=[f"model-{kind}"],
        source=PluginSource.USER,
        tier="user",
        dependencies=[],
        min_ea_version="0.1.0",
        engines={},
        # Registration IS activation: model-installed contributions join the
        # always-active core set, never a deferred zone.
        activation="core",
        activation_events=[],
        permissions=["tools:register"],
        provides_tools=provides_tools or [],
        provides_skills=provides_skills or [],
        provides_mcp=provides_mcp or [],
        provides_agent=provides_agent or [],
    )
    return _DynamicPlugin(
        manifest,
        on_activate_fn=on_activate_fn,
        on_deactivate_fn=on_deactivate_fn,
    )


def _register_and_activate(registry: Any, plugin: Any) -> str | None:
    """Register and activate a dynamic plugin; return an error string or None."""
    try:
        registry.register(plugin)
    except Exception as e:
        return f"plugin registration failed: {e}"
    if not registry.activate(plugin.manifest.name, host=registry.default_host_dict()):
        reason = registry.list_failed().get(plugin.manifest.name, "activation failed")
        registry.unregister(plugin.manifest.name)
        return f"plugin activation failed: {reason}"
    return None


async def _install_tool(_loop: Any, kwargs: dict[str, Any]) -> str:
    """Install a model-authored tool as a plugin (lifecycle-managed)."""
    name = (kwargs.get("name") or "").strip()
    description = (kwargs.get("description") or "").strip()
    input_schema_raw = kwargs.get("input_schema")
    code = (kwargs.get("code") or "").strip()

    if not name or not description or not input_schema_raw or not code:
        return "Error: install_tool requires 'name', 'description', 'input_schema', and 'code'."

    if isinstance(input_schema_raw, str):
        try:
            input_schema = json.loads(input_schema_raw)
        except json.JSONDecodeError:
            return "Error: input_schema is not valid JSON."
    else:
        input_schema = input_schema_raw

    if not isinstance(input_schema, dict):
        return "Error: input_schema must be a JSON object (JSON Schema)."

    local_ns: dict[str, Any] = {"__import__": __import__}
    try:
        exec(code, local_ns)
    except Exception as e:
        return f"Error: failed to compile tool code: {e}"

    execute_fn = local_ns.get("execute")
    if execute_fn is None or not callable(execute_fn):
        return "Error: code must define an 'execute' async function."

    category = (kwargs.get("category") or "").strip() or None
    intents_raw = kwargs.get("intents")
    intents = None
    if intents_raw:
        if isinstance(intents_raw, list):
            intents = [str(i) for i in intents_raw]
        elif isinstance(intents_raw, str):
            intents = [s.strip() for s in intents_raw.split(",") if s.strip()]

    always_available = kwargs.get("always_available", False)
    if isinstance(always_available, str):
        always_available = always_available.lower() in ("true", "1", "yes")

    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    plugin_name = f"model-tool-{name}"
    if registry.get_manifest(plugin_name) is not None:
        return json.dumps(
            {
                "status": "already_exists",
                "name": name,
                "plugin": plugin_name,
                "message": f"Tool '{name}' is already installed as '{plugin_name}'.",
            },
            ensure_ascii=False,
            indent=2,
        )

    try:
        tool = build_tool(
            name=name,
            description=description,
            input_schema=input_schema,
            execute=execute_fn,
            category=category,
            intents=intents,
            always_available=always_available,
            source="model",
        )
    except Exception as e:
        return f"Error: failed to build tool '{name}': {e}"

    plugin = _make_dynamic_plugin(
        "tool",
        name,
        description,
        provides_tools=[name],
        on_activate_fn=lambda ctx: ctx.tools.register(tool),
    )
    err = _register_and_activate(registry, plugin)
    if err:
        return f"Error: failed to install tool '{name}': {err}"

    _record_manage_op(_loop, "install_tool", name=name, plugin_name=plugin_name)

    return json.dumps(
        {
            "status": "installed",
            "name": name,
            "plugin": plugin_name,
            "managed": True,
            "available_now": True,
            "note": "Managed as a plugin: disable/uninstall it via manage(plugin_disable/plugin_uninstall).",
        },
        ensure_ascii=False,
        indent=2,
    )


async def _install_agent(_loop: Any, kwargs: dict[str, Any]) -> str:
    """Install a named sub-agent as a plugin (lifecycle-managed)."""
    name = (kwargs.get("name") or "").strip()
    description = (kwargs.get("description") or "").strip()
    system_prompt = (kwargs.get("system_prompt") or "").strip()
    tool_policy = (kwargs.get("tool_policy") or "all").strip()

    if not name or not description:
        return "Error: install_agent requires 'name' and 'description'."

    if tool_policy not in ("all", "readonly", "no_writes"):
        return f"Error: tool_policy must be 'all', 'readonly', or 'no_writes', got '{tool_policy}'."

    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    plugin_name = f"model-agent-{name}"
    if registry.get_manifest(plugin_name) is not None:
        return json.dumps(
            {
                "status": "already_exists",
                "name": name,
                "plugin": plugin_name,
                "message": f"Agent '{name}' is already installed as '{plugin_name}'.",
            },
            ensure_ascii=False,
            indent=2,
        )

    agent_def = {
        "name": name,
        "description": description,
        "system_prompt": system_prompt,
        "tool_policy": tool_policy,
        "source": "model",
    }
    plugin = _make_dynamic_plugin(
        "agent",
        name,
        description,
        provides_agent=[agent_def],
        on_activate_fn=lambda ctx: ctx.sub_agents.register(agent_def),
    )
    err = _register_and_activate(registry, plugin)
    if err:
        return f"Error: failed to install agent '{name}': {err}"

    _record_manage_op(_loop, "install_agent", name=name, plugin_name=plugin_name)

    return json.dumps(
        {
            "status": "installed",
            "name": name,
            "plugin": plugin_name,
            "tool_policy": tool_policy,
            "managed": True,
            "available_now": True,
        },
        ensure_ascii=False,
        indent=2,
    )


async def _install_skill(loop: Any, kwargs: dict[str, Any]) -> str:
    """Install a skill as a plugin (lifecycle-managed)."""
    from encre.skills.types import BundledSkillDefinition, SkillSource

    name = (kwargs.get("name") or "").strip()
    description = (kwargs.get("description") or "").strip()
    body = (kwargs.get("body") or "").strip()
    aliases_raw = kwargs.get("aliases")

    if not name or not description or not body:
        return "Error: install_skill requires 'name', 'description', and 'body'."

    skill_registry = getattr(loop, "skill_registry", None)
    if skill_registry is None:
        return "Error: no skill_registry available on this loop."

    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    plugin_name = f"model-skill-{name}"
    if registry.get_manifest(plugin_name) is not None:
        return json.dumps(
            {
                "status": "already_exists",
                "name": name,
                "plugin": plugin_name,
                "message": f"Skill '{name}' is already installed as '{plugin_name}'.",
            },
            ensure_ascii=False,
            indent=2,
        )

    aliases = []
    if aliases_raw:
        if isinstance(aliases_raw, list):
            aliases = [str(a) for a in aliases_raw]
        elif isinstance(aliases_raw, str):
            aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]

    async def _get_prompt(_args: str | None, _context: dict[str, Any]) -> str:
        return body

    skill = BundledSkillDefinition(
        name=name,
        description=description,
        get_prompt_for_command=_get_prompt,
        aliases=aliases,
        source=SkillSource.MANAGED,
        body=body,
    )

    def _activate(_ctx: Any) -> None:
        skill_registry.register(skill)

    def _deactivate() -> None:
        with contextlib.suppress(Exception):
            skill_registry.unregister(name)

    plugin = _make_dynamic_plugin(
        "skill",
        name,
        description,
        provides_skills=[name],
        on_activate_fn=_activate,
        on_deactivate_fn=_deactivate,
    )
    err = _register_and_activate(registry, plugin)
    if err:
        return f"Error: failed to install skill '{name}': {err}"

    _record_manage_op(loop, "install_skill", name=name, plugin_name=plugin_name)

    return json.dumps(
        {
            "status": "installed",
            "name": name,
            "plugin": plugin_name,
            "aliases": aliases,
            "managed": True,
            "available_now": True,
        },
        ensure_ascii=False,
        indent=2,
    )


async def _install_mcp(loop: Any, kwargs: dict[str, Any]) -> str:
    """Connect an MCP server and register it as a plugin (lifecycle-managed)."""
    from encre.tools.mcp import EncreMCPTool

    name = (kwargs.get("name") or "").strip()
    command = (kwargs.get("command") or "").strip()
    url = (kwargs.get("url") or "").strip()
    args_raw = kwargs.get("args")

    if not name:
        return "Error: install_mcp requires 'name'."
    if not command and not url:
        return "Error: install_mcp requires 'command' (stdio) or 'url' (HTTP)."
    if command and url:
        return "Error: specify either 'command' (stdio) or 'url' (HTTP), not both."

    args = []
    if args_raw:
        if isinstance(args_raw, list):
            args = [str(a) for a in args_raw]
        elif isinstance(args_raw, str):
            args = [a.strip() for a in args_raw.split(" ") if a.strip()]

    env = kwargs.get("env")
    cwd = kwargs.get("cwd")

    registry = _plugin_registry()
    if registry is None:
        return "Error: plugin registry is not available in this process."
    plugin_name = f"model-mcp-{name}"
    if registry.get_manifest(plugin_name) is not None:
        return json.dumps(
            {
                "status": "already_exists",
                "name": name,
                "plugin": plugin_name,
                "message": f"MCP server '{name}' is already installed as '{plugin_name}'.",
            },
            ensure_ascii=False,
            indent=2,
        )

    mcp_entry: dict[str, Any] = {"name": name, "source": "model"}
    if command:
        mcp_entry["type"] = "stdio"
        mcp_entry["command"] = command
        if args:
            mcp_entry["args"] = args
    else:
        mcp_entry["type"] = "http"
        mcp_entry["url"] = url
    if env:
        mcp_entry["env"] = env
    if cwd:
        mcp_entry["cwd"] = cwd

    # Connect first (async), then hand the live tool object to the plugin's
    # deactivate hook so disable/uninstall tears the connection down.
    try:
        mcp_tool = EncreMCPTool(
            command=[command, *args] if command else [],
            server_url=url if url else "",
            env=env if env and isinstance(env, dict) else None,
            cwd=cwd,
            http_timeout=float(kwargs.get("timeout", 60.0)),
        )
        await mcp_tool.register_with(loop.tool_registry, prefix="mcp__")
    except Exception as e:
        return f"Error: failed to connect MCP server '{name}': {e}"

    if not hasattr(loop, "_mcp_tools"):
        loop._mcp_tools = []
    loop._mcp_tools.append(mcp_tool)

    def _deactivate() -> None:
        with contextlib.suppress(Exception):
            loop._mcp_tools.remove(mcp_tool)

    plugin = _make_dynamic_plugin(
        "mcp",
        name,
        f"MCP server '{name}' ({mcp_entry['type']})",
        provides_mcp=[mcp_entry],
        on_deactivate_fn=_deactivate,
    )
    err = _register_and_activate(registry, plugin)
    if err:
        # Roll the live connection back so a failed install leaves nothing.
        with contextlib.suppress(Exception):
            await mcp_tool.disconnect()
        with contextlib.suppress(Exception):
            loop._mcp_tools.remove(mcp_tool)
        return f"Error: failed to register MCP plugin '{name}': {err}"

    _record_manage_op(loop, "install_mcp", name=name, plugin_name=plugin_name)

    return json.dumps(
        {
            "status": "installed",
            "name": name,
            "plugin": plugin_name,
            "transport": mcp_entry["type"],
            "managed": True,
            "available_now": True,
            "note": "Managed as a plugin: the server auto-reconnects after plugin restarts.",
        },
        ensure_ascii=False,
        indent=2,
    )


manage_tool = build_tool(
    name="manage",
    description=(
        "Plugin manager for the runtime: search the tool index for existing "
        "capabilities, install new tools / sub-agents / skills / MCP "
        "servers, and manage the full plugin lifecycle (list, install, "
        "uninstall, enable, disable, reload a single plugin, or restart the "
        "whole plugin system). MCP servers, skills, and agents are all "
        "defined as plugins, so these install actions create plugin-backed "
        "capabilities. All changes take effect immediately and are "
        "available on the very next turn.\n\n"
        "WHEN to use: before installing anything, run 'discover' with a "
        "natural-language description of the capability you need -- a "
        "matching tool (database, browser, docker, ssh, email, ...) may "
        "already exist and is already callable. Use 'plugin_list' to "
        "inspect installed plugins and PENDING third-party plugins awaiting "
        "confirmation; 'plugin_install' confirms a pending plugin (this "
        "executes third-party code -- review its permissions first). Use "
        "the install_* actions only when no existing plugin covers the "
        "need.\n"
        "WHEN NOT to use: if a built-in tool already covers the task, prefer "
        "it over installing a new one; do not disable or uninstall plugins "
        "without a reason -- there is no protected tier, so any plugin "
        "(bundled or third-party) can be disabled or uninstalled and will "
        "not come back until re-enabled or reinstalled.\n"
        'TIPS: describe the capability in natural language (e.g. "run SQL '
        "queries\"), not a tool name; pass 'category' to narrow the discover "
        "search; keep new tool code self-contained (the `code` field is "
        "exec'd in a fresh namespace and must define an `async def "
        "execute(**kwargs)`); give agents a focused system_prompt and a "
        "narrow tool_policy to limit blast radius; verify MCP server "
        "connectivity with a trivial call right after install_mcp; after "
        "plugin_restart check the returned 'failed' map.\n"
        "PITFALLS: very vague discover queries may return too many "
        "irrelevant matches; install_tool runs arbitrary Python via exec -- "
        "only install code you trust; install_mcp with stdio spawns a "
        "long-lived child process, so prefer HTTP transport when available; "
        "plugin_disable persists across restarts for user-installed plugins "
        "(session-only otherwise).\n\n"
        "Actions:\n"
        "  discover        - Search the tool index and report matches "
        "(pure information lookup: all registered tools are already active "
        "and callable; this only tells you what exists and its parameters). "
        "Requires: query. Optional: top_k (default 5, max 15), category "
        "(filesystem/search/shell/web/infra/data/code_intel/gui/docs/media/"
        "memory/task/delegation).\n"
        "  install_tool    - Create and register a brand new tool. "
        "Requires: name, description, input_schema (JSON Schema), "
        "code (Python async function body named `execute`). "
        "Optional: category, intents, always_available.\n"
        "  install_agent   - Register a new named sub-agent. "
        "Requires: name, description. "
        "Optional: system_prompt, tool_policy (all/readonly/no_writes).\n"
        "  install_skill   - Install a new skill. "
        "Requires: name, description, body (skill prompt text). "
        "Optional: aliases.\n"
        "  install_mcp     - Connect a new MCP server. "
        "Requires: name, and either command (stdio) or url (HTTP). "
        "Optional: args, env, cwd, timeout.\n"
        "  plugin_list     - List all plugins with state/enabled/tier, "
        "failed reasons, and PENDING items awaiting confirmation.\n"
        "  plugin_install  - Confirm and activate a PENDING plugin "
        "(executes its code). Requires: name.\n"
        "  plugin_activate - Activate a registered plugin (logical, "
        "non-destructive: merges contributions into the live runtime). "
        "Requires: name.\n"
        "  plugin_uninstall - Logical uninstall (deactivate, unregister, "
        "ledger-remove; no files are deleted). Requires: name.\n"
        "  plugin_enable   - Re-enable a disabled plugin. Requires: name.\n"
        "  plugin_disable  - Disable a plugin (persists for user-installed "
        "plugins). Requires: name.\n"
        "  plugin_reload   - Hot-reload one plugin (deactivate, re-import, "
        "re-activate). Requires: name.\n"
        "  plugin_restart  - Restart the whole plugin system (deactivate "
        "all, rediscover, reactivate). No arguments."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "discover",
                    "install_tool",
                    "install_agent",
                    "install_skill",
                    "install_mcp",
                    "plugin_list",
                    "plugin_install",
                    "plugin_activate",
                    "plugin_uninstall",
                    "plugin_enable",
                    "plugin_disable",
                    "plugin_reload",
                    "plugin_restart",
                ],
                "description": (
                    "What to do: discover (search the tool index -- report "
                    "only, everything found is already active), install/"
                    "register a new tool, sub-agent, skill, or MCP server "
                    "connection, or manage the plugin lifecycle (list/"
                    "install/activate/uninstall/enable/disable/reload one "
                    "plugin, or restart the whole plugin system)."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Natural-language description of the capability you need (for discover). Describe what you want to do, not a tool name."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": "Max discover results to return (default 5, max 15).",
            },
            "name": {
                "type": "string",
                "description": (
                    "Name of the tool/agent/skill/MCP server to install, or "
                    "the plugin to manage (for plugin_install, "
                    "plugin_uninstall, plugin_enable, plugin_disable, "
                    "plugin_reload)."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Human-readable description (for tools, agents, skills). "
                    "The model reads this to decide when to use the new "
                    "capability, so be specific."
                ),
            },
            "input_schema": {
                "oneOf": [{"type": "object"}, {"type": "string"}],
                "description": (
                    "JSON Schema describing the new tool's parameters (for "
                    "install_tool). May be a dict or a JSON string. Required "
                    "for install_tool."
                ),
            },
            "code": {
                "type": "string",
                "description": (
                    "Python source that defines an `async def "
                    "execute(**kwargs)` function for the new tool (for "
                    "install_tool). Executed in a fresh namespace; must "
                    "return a string. Required for install_tool."
                ),
            },
            "category": {
                "type": "string",
                "description": "Category label for the new tool, e.g. 'web', 'file', 'system' (for install_tool).",
            },
            "intents": {
                "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}],
                "description": (
                    "Intent tags controlling when the tool is surfaced (for install_tool). Pass as an array or a comma-separated string."
                ),
            },
            "always_available": {
                "type": "boolean",
                "description": "If true, the installed tool is always visible without discovery (default false, for install_tool).",
            },
            "system_prompt": {
                "type": "string",
                "description": "System prompt defining the sub-agent's persona and instructions (for install_agent).",
            },
            "tool_policy": {
                "type": "string",
                "enum": ["all", "readonly", "no_writes"],
                "description": (
                    "Tool access policy for the sub-agent (for "
                    "install_agent): 'all' (default) grants everything, "
                    "'readonly' blocks write tools, 'no_writes' blocks "
                    "destructive ones."
                ),
            },
            "body": {
                "type": "string",
                "description": "Skill prompt body -- the instruction text returned when the skill is invoked (for install_skill).",
            },
            "aliases": {
                "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}],
                "description": "Alternative trigger names for the skill (for install_skill). Array or comma-separated string.",
            },
            "command": {
                "type": "string",
                "description": (
                    "Executable command to launch a stdio MCP server, e.g. "
                    "'npx' or 'python -m' (for install_mcp). Mutually "
                    "exclusive with 'url'."
                ),
            },
            "url": {
                "type": "string",
                "description": "HTTP/SSE URL of a remote MCP server (for install_mcp). Mutually exclusive with 'command'.",
            },
            "args": {
                "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}],
                "description": (
                    "Command-line arguments appended to 'command' for a "
                    "stdio MCP server (for install_mcp). Array or "
                    "space-separated string."
                ),
            },
            "env": {
                "type": "object",
                "description": "Environment variables to set for the MCP server process (for install_mcp). Use for API keys and config.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory in which to launch the MCP server process (for install_mcp).",
            },
            "timeout": {
                "type": "number",
                "description": "Connection/initialization timeout in seconds for the MCP server (for install_mcp, default 60).",
            },
        },
        "required": ["action"],
    },
    execute=_manage_execute,
    intents=["general", "coding", "system"],
    category="meta",
    triggers=[
        "find tool",
        "search tools",
        "discover tool",
        "install",
        "manage",
        "register",
        "deploy",
        "god",
        "create tool",
        "new tool",
        "plugin",
        "uninstall plugin",
        "disable plugin",
        "enable plugin",
        "reload plugin",
        "restart plugins",
        "plugin manager",
    ],
    always_available=True,
    is_concurrency_safe=lambda _: False,
    is_readonly=False,
    is_destructive=True,
)
