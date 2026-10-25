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

"""Gateway slash-command actions for IM adapters.

The adapter base class detects leading-``/`` text and emits it through the
hook registry as ``command:<name>`` (see
:mod:`encre.gateway.platforms.base`).  This module owns the *actions* behind
those events: an extensible registry mapping canonical command names to async
handlers, plus the built-in ``new`` and ``model`` commands.

Design notes:

* **Extensible.**  New commands are one :func:`GatewayCommandRegistry.register`
  call with a :class:`CommandAction`.  Nothing else needs to change.
  Every command has exactly one name; aliases are not supported.
* **Self-describing, stateless interaction.**  Commands that need a choice
  (``/model``) reply with complete follow-up commands (``/model <id>``) instead
  of parking server-side selection state, so the interaction survives latency,
  restarts, and platforms without interactive widgets.
* **Safe degradation.**  An unregistered ``/xxx`` resolves to no action and the
  dispatcher returns ``None``; the adapter base class then treats the text as a
  normal agent message.  Unknown commands must never be swallowed.
* **English-only replies.**  Every user-visible reply from this layer is fixed
  English regardless of the user's language setting.  i18n belongs to the
  desktop renderer; the gateway (IM/CLI adapters) must not grow language packs.

A handler receives a :class:`CommandContext` and returns a hook decision dict
(``{"decision": "handled"}`` after it has replied itself) or ``None`` to let
the text fall through to the agent.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from encre.gateway.hooks import COMMAND_WILDCARD, SESSION_RESET, get_hook_registry
from encre.gateway.session import SessionSource, build_session_key
from encre.slash_commands import BUILTIN_SLASH_COMMANDS

logger = logging.getLogger("encre.gateway.commands")

# Modes that loop.set_mode accepts; anything else clears the mode.
_VALID_MODES = ("", "plan", "spec")


@dataclass
class CommandContext:
    """Everything a command handler needs to act and reply.

    Attributes:
        runner: The :class:`~encre.gateway.run.GatewayRunner` owning the
            session store and adapter instances.
        adapter: The adapter that received the command; used to send replies.
        source: Rebuilt conversation identity for the sending chat/thread.
        session_key: Stable conversation key derived from ``source``.
        command: Canonical (lowercased) command name without the slash.
        args: Everything after the command token, whitespace-collapsed.
        text: The raw message text, including the leading slash.
    """

    runner: Any
    adapter: Any
    source: SessionSource
    session_key: str
    command: str
    args: str
    text: str


CommandHandler = Callable[[CommandContext], Awaitable[dict[str, Any] | None]]


@dataclass(frozen=True)
class CommandAction:
    """One gateway command: metadata plus its async handler.

    Attributes:
        name: Canonical name, matched case-insensitively without the slash.
        description: One-line summary, shown in help output.
        handler: Async callable turning a :class:`CommandContext` into a hook
            decision dict (or ``None`` to fall through).
        argument_hint: Human-readable argument shape, e.g. ``"<model-id>"``.
            Empty for commands that take no arguments.
    """

    name: str
    description: str
    handler: CommandHandler
    argument_hint: str = ""


class GatewayCommandRegistry:
    """Name -> :class:`CommandAction` map for gateway commands.

    Registration happens at gateway startup (built-ins) and may also happen
    later (plugins, future user command files).  Reads are plain dict lookups,
    so no locking is required beyond the usual "do not register while
    dispatching" convention shared with :class:`~encre.gateway.hooks.HookRegistry`.
    """

    def __init__(self) -> None:
        self._actions: dict[str, CommandAction] = {}

    def register(self, action: CommandAction) -> None:
        """Add *action* under its canonical name.

        Later registrations win for a duplicate name.

        Args:
            action: The command action to expose.
        """
        self._actions[action.name.lower()] = action

    def resolve(self, name: str) -> CommandAction | None:
        """Look up an action by name (case-insensitive).

        Args:
            name: The typed command token without the leading slash.

        Returns:
            The action, or ``None`` when the name is unknown -- which is the
            dispatcher's signal to let the text reach the agent unchanged.
        """
        return self._actions.get(name.lower())

    def list_actions(self) -> list[CommandAction]:
        """Return all registered actions in registration order."""
        return list(self._actions.values())


def _resolve_custom_command(runner: Any, name: str, source: SessionSource | None) -> Any:
    """Resolve a user/project-defined slash command by name.

    User commands live in settings.json (``custom_slash_commands``) and in
    home-directory ``*.md`` folders; project commands in the workspace's
    ``.encre/commands`` / ``.claude/commands``.  The live session's agent
    registry (when the conversation already has one) is preferred because it
    also carries project commands; otherwise a fresh registry with builtins
    plus user commands is built.  Rebuilding per invocation is intentional:
    command files are few and IM commands are rare, and this keeps edits
    visible without a restart.

    Args:
        runner: The gateway runner (used to reach a live session's registry).
        name: The typed command name without the leading slash.
        source: Conversation identity, used to find a live session.

    Returns:
        The matching :class:`~encre.slash_commands.SlashCommandDef`, or None.
    """
    from encre.slash_commands import EncreCommandRegistry, load_user_commands

    registry = None
    session_id = None
    if source is not None:
        store = getattr(runner, "session_store", None)
        if store is not None:
            session_id = store.get(source)
    if session_id:
        router = getattr(runner, "router", None)
        info = router.session_manager.get_session(session_id) if router is not None else None
        registry = getattr(getattr(info, "agent", None), "command_registry", None) if info is not None else None
    if registry is None:
        registry = load_user_commands(EncreCommandRegistry())
    return registry.lookup(name)


def _dispatch_custom_command(ctx: dict[str, Any], runner: Any, command: str) -> dict[str, Any] | None:
    """Fall back to user/project-defined commands for unknown gateway names.

    A matching command with a prompt body is rewritten into that rendered
    prompt so the normal agent pipeline executes it (same path as ``/init``).
    Commands without a body return ``None`` -- the text falls through to the
    agent unchanged, which is the established safe degradation.

    Args:
        ctx: The hook context dict built by the adapter base class.
        runner: The gateway runner the command belongs to.
        command: Canonical (lowercased) command name without the slash.

    Returns:
        A ``rewrite`` decision when a custom command matched, else ``None``.
    """
    raw_source = ctx.get("source")
    source = SessionSource.from_dict(raw_source) if isinstance(raw_source, dict) else None
    custom = _resolve_custom_command(runner, command, source)
    if custom is None:
        return None
    body = custom.render_prompt(str(ctx.get("args", "")))
    if not body:
        logger.debug("[commands] /%s is defined but has no prompt body", command)
        return None
    return {"decision": "rewrite", "text": body}


async def dispatch_command(ctx: dict[str, Any], runner: Any, registry: GatewayCommandRegistry) -> dict[str, Any] | None:
    """Route one ``command:<name>`` hook event to its action.

    Args:
        ctx: The hook context dict built by the adapter base class (``command``,
            ``text``, ``args``, ``adapter``, and a ``source`` snapshot when the
            event carried one).
        runner: The :class:`~encre.gateway.run.GatewayRunner` the command
            belongs to.
        registry: The registry to resolve the command name against.

    Returns:
        The action's decision dict, or ``None`` when the command is unknown or
        carries no resolvable conversation source -- both mean "treat the text
        as a normal agent message".
    """
    command = str(ctx.get("command", "")).lower()
    action = registry.resolve(command)
    if action is None:
        # Not a gateway built-in: try user/project-defined commands (settings
        # JSON or *.md files) before degrading to a normal agent message.
        return _dispatch_custom_command(ctx, runner, command)
    raw_source = ctx.get("source")
    if not isinstance(raw_source, dict):
        # Without a conversation identity there is nothing to act on; the
        # safe behaviour is degradation to a normal message, not an error.
        logger.debug("[commands] /%s has no source snapshot, ignoring", command)
        return None
    source = SessionSource.from_dict(raw_source)
    adapter_name = str(ctx.get("adapter", ""))
    adapter = runner.get_adapter(adapter_name)
    if adapter is None:
        logger.warning("[commands] /%s from unknown adapter '%s', ignoring", command, adapter_name)
        return None
    cctx = CommandContext(
        runner=runner,
        adapter=adapter,
        source=source,
        session_key=build_session_key(source),
        command=command,
        args=str(ctx.get("args", "")),
        text=str(ctx.get("text", "")),
    )
    try:
        return await action.handler(cctx)
    except Exception as e:
        logger.error("[commands] /%s handler failed: %s %s", command, type(e).__name__, e, exc_info=True)
        try:
            await adapter.send(source.chat_id, "⚠️ Command failed, please try again later.")
        except Exception as send_err:
            logger.warning("[commands] Failed to send command error notice: %s", send_err)
        return {"decision": "handled"}


# -- Built-in commands -------------------------------------------------------


async def _cmd_new(ctx: CommandContext) -> dict[str, Any]:
    """``/new``: start a fresh conversation for this chat/thread.

    Drops the routing row (so the next message creates a brand-new agent
    session; history is kept server-side) and clears the per-conversation
    model pin, then confirms in-band.  Replies are best-effort: a send
    failure must not resurrect the old session by making the caller think
    the command failed.

    Args:
        ctx: The resolved command context.

    Returns:
        ``{"decision": "handled"}`` -- the command never reaches the agent.
    """
    store = ctx.runner.session_store
    store.reset(ctx.source)
    store.set_preferred_model(ctx.session_key, None)
    # A fresh conversation also starts in normal mode, so drop any mode
    # requested before it existed.
    store.set_active_mode(ctx.session_key, None)
    try:
        await get_hook_registry().emit(
            SESSION_RESET,
            {
                "session_key": ctx.session_key,
                "adapter": ctx.adapter.name,
                "chat_id": ctx.source.chat_id,
                "user_id": ctx.source.user_id,
                "reason": "command",
            },
        )
    except Exception as e:
        logger.warning("[commands] session:reset emit failed: %s", e)
    try:
        await ctx.adapter.send(ctx.source.chat_id, "✅ New session started.")
    except Exception as send_err:
        logger.warning("[commands] Failed to send /new confirmation: %s", send_err)
    return {"decision": "handled"}


async def _load_enabled_model_ids() -> list[str]:
    """Return the enabled model ids from the live persisted config.

    Reads the same config store the desktop settings write to, so the list is
    always current without holding a long-lived config snapshot.

    Returns:
        Enabled model ids in priority order (indicator model first), or an
        empty list when the config cannot be read.
    """
    from encre.config import EncreConfig

    cfg = EncreConfig.from_file()
    return [m.model_id for m in cfg.get_enabled_models() if m.model_id]


async def _cmd_model(ctx: CommandContext) -> dict[str, Any]:
    """``/model``: show or pin the model used by this conversation.

    Without arguments the handler lists every enabled model, one complete
    ``/model <id>`` command per line, and marks the current pin -- the reply
    itself is the picker, so no server-side selection state is needed.  With
    an argument the id is validated against the live config and pinned for
    this conversation only; subsequent runs hand it to the agent as the first
    target model.

    Args:
        ctx: The resolved command context.

    Returns:
        ``{"decision": "handled"}`` in every path: the command is always
        answered by the handler itself, never forwarded to the agent.
    """
    store = ctx.runner.session_store
    current = store.get_preferred_model(ctx.session_key)
    args = ctx.args.strip()

    try:
        model_ids = await _load_enabled_model_ids()
    except Exception as e:
        logger.warning("[commands] Failed to load models for /model: %s", e)
        model_ids = []
        if not args:
            await ctx.adapter.send(ctx.source.chat_id, "⚠️ Unable to load the model list.")
            return {"decision": "handled"}

    if not args:
        lines = ["Available models:"]
        for mid in model_ids:
            marker = " (current)" if mid == current else ""
            lines.append(f"/model {mid}{marker}")
        lines.append(f"Current: {current or 'auto'}")
        # Paragraph breaks instead of single newlines -- see _cmd_help.
        await ctx.adapter.send(ctx.source.chat_id, "\n\n".join(lines))
        return {"decision": "handled"}

    model_id = args.split()[0]
    if model_id not in model_ids:
        await ctx.adapter.send(
            ctx.source.chat_id,
            f"⚠️ Unknown or disabled model '{model_id}'.\nSend /model to list available models.",
        )
        return {"decision": "handled"}
    store.set_preferred_model(ctx.session_key, model_id)
    await ctx.adapter.send(ctx.source.chat_id, f"✅ Model set to {model_id} for this conversation.")
    return {"decision": "handled"}


def apply_session_mode(runner: Any, session_id: str, mode: str) -> bool:
    """Set the slash-command mode on a live agent session.

    Mirrors what the desktop ``set_mode`` path does minus the WS broadcast:
    drives ``loop.set_mode`` and updates the ``slash_command_mode`` metadata
    mirror, which is also what a reloaded session restores its mode from --
    so no gateway-specific persistence is needed once this succeeds.

    Args:
        runner: The gateway runner (used to reach the session manager).
        session_id: The agent session to update.
        mode: ``"plan"``, ``"spec"``, or ``""`` to clear; anything else is
            treated as clearing.

    Returns:
        True when the session was live and the mode was applied; False when
        the session is not currently loaded (caller should pend the request).
    """
    router = getattr(runner, "router", None)
    if router is None:
        return False
    info = router.session_manager.get_session(session_id)
    agent = getattr(info, "agent", None) if info is not None else None
    if agent is None:
        return False
    effective = mode if mode in _VALID_MODES else ""
    agent.loop.set_mode(effective)
    underlying = getattr(agent, "session", None)
    if underlying is not None:
        try:
            underlying.metadata["slash_command_mode"] = effective
        except Exception:
            logger.debug("failed to mirror slash_command_mode metadata", exc_info=True)
    return True


async def _cmd_set_mode(ctx: CommandContext, mode: str, label: str) -> dict[str, Any]:
    """Shared body of ``/plan`` and ``/spec``: switch this conversation's mode.

    When the conversation already has a live session the mode is applied
    immediately; otherwise it is stored as a pending preference that the
    gateway applies right before the next run creates/resumes the session,
    so ``/plan`` works on a brand-new chat too.  ``/plan off`` (or ``normal``)
    returns the conversation to normal mode.

    Args:
        ctx: The resolved command context.
        mode: The target mode, ``"plan"`` or ``"spec"``.
        label: Human-readable mode name used in the confirmation reply.

    Returns:
        ``{"decision": "handled"}`` -- always answered in-band.
    """
    store = ctx.runner.session_store
    args = ctx.args.strip().lower()
    effective = "" if args in ("off", "normal", "none") else mode
    applied = False
    session_id = store.get(ctx.source)
    if session_id:
        applied = apply_session_mode(ctx.runner, session_id, effective)
    if not applied:
        # No live session: remember the request; the next inbound message
        # applies it before the run starts (see GatewayRunner).
        store.set_active_mode(ctx.session_key, effective or None)
    if effective:
        await ctx.adapter.send(ctx.source.chat_id, f"✅ {label} mode enabled for this conversation. Send /{mode} off to go back.")
    else:
        await ctx.adapter.send(ctx.source.chat_id, "✅ Back to normal mode.")
    return {"decision": "handled"}


async def _cmd_init(ctx: CommandContext) -> dict[str, Any]:
    """``/init``: run the workspace-initialization prompt through the agent.

    Reuses the harness builtin command definition as the single source of
    truth: its prompt body is rendered with the command arguments and handed
    back as a ``rewrite`` decision, so the adapter base class replaces the
    ``/init`` text with the prompt and the normal agent pipeline takes over
    (streaming, per-session serialization, delivery -- all unchanged).

    Args:
        ctx: The resolved command context.

    Returns:
        A ``rewrite`` decision carrying the rendered prompt, or ``handled``
        if the prompt body is unavailable.
    """
    init_def = next((c for c in BUILTIN_SLASH_COMMANDS if c.name == "init"), None)
    body = init_def.render_prompt(ctx.args) if init_def is not None else ""
    if not body:
        await ctx.adapter.send(ctx.source.chat_id, "⚠️ Init prompt is not available.")
        return {"decision": "handled"}
    return {"decision": "rewrite", "text": body}


def _conversation_session_labels(runner: Any, ids: list[str]) -> dict[str, str]:
    """Best-effort name/preview for each session id (empty string if unknown).

    Prefers the resident record, falls back to the persisted index; a session
    parked in another directory simply renders without a label.
    """
    labels: dict[str, str] = {}
    router = getattr(runner, "router", None)
    manager = getattr(router, "session_manager", None) if router is not None else None
    if manager is None:
        return labels
    for sid in ids:
        label = ""
        try:
            info = manager.get_session(sid)
            if info is not None:
                label = str((info.metadata or {}).get("name") or "")
            if not label:
                entry = manager.index_entry(sid)
                if isinstance(entry, dict):
                    label = str(entry.get("name") or entry.get("preview") or "")
        except Exception:
            label = ""
        labels[sid] = " ".join(label.split())[:48]
    return labels


async def _cmd_sessions(ctx: CommandContext) -> dict[str, Any]:
    """``/sessions``: list this conversation's own switchable sessions.

    Scope comes from the routing key derived from the message source, so an
    IM chat only ever sees (and can only resume) sessions it created itself
    -- desktop normal/iwork sessions and other chats' sessions are outside
    the key namespace entirely.
    """
    store = ctx.runner.session_store
    current = store.get(ctx.source)
    ids = store.sessions_for(ctx.source)
    if not ids:
        await ctx.adapter.send(
            ctx.source.chat_id,
            "This conversation has no sessions yet; send a message first.",
        )
        return {"decision": "handled"}
    labels = _conversation_session_labels(ctx.runner, ids)
    lines = ["Your sessions (newest first):"]
    for sid in ids:
        marker = "  <- current" if sid == current else ""
        lines.append(f"- {sid[:8]}  {labels.get(sid) or '(untitled)'}{marker}")
    lines.append("Switch with /resume <id>.")
    await ctx.adapter.send(ctx.source.chat_id, "\n\n".join(lines))
    return {"decision": "handled"}


async def _cmd_resume(ctx: CommandContext) -> dict[str, Any]:
    """``/resume <id>``: re-point this conversation to one of its sessions.

    Selection is by session id only -- positional numbers are deliberately
    not supported, since the list shifts whenever a new session is created
    and a stale number could silently resume the wrong transcript.  The
    target must appear in :meth:`SessionStore.sessions_for` for this exact
    source; unknown or foreign ids are refused with guidance rather than
    guessed.  ``put`` archives the displaced session so the switch is
    reversible.
    """
    arg = ctx.args.strip().split(maxsplit=1)[0] if ctx.args.strip() else ""
    store = ctx.runner.session_store
    if not arg:
        await ctx.adapter.send(
            ctx.source.chat_id,
            "Usage: /resume <session id>  -- list options with /sessions.",
        )
        return {"decision": "handled"}
    ids = store.sessions_for(ctx.source)
    if not ids:
        await ctx.adapter.send(
            ctx.source.chat_id,
            "This conversation has no sessions to switch between yet.",
        )
        return {"decision": "handled"}
    hits = [sid for sid in ids if sid.startswith(arg.lower())]
    if len(hits) > 1:
        await ctx.adapter.send(
            ctx.source.chat_id,
            f"'{arg}' matches {len(hits)} sessions; use a longer id from /sessions.",
        )
        return {"decision": "handled"}
    if not hits:
        await ctx.adapter.send(
            ctx.source.chat_id,
            f"Session '{arg}' is not part of this conversation. /sessions lists the ones you can switch to.",
        )
        return {"decision": "handled"}
    target = hits[0]
    current = store.get(ctx.source)
    if target == current:
        await ctx.adapter.send(ctx.source.chat_id, f"Already on session {target[:8]}.")
        return {"decision": "handled"}
    store.put(ctx.source, target)
    await ctx.adapter.send(
        ctx.source.chat_id,
        f"✅ Switched to session {target[:8]}. Your next message continues it.",
    )
    return {"decision": "handled"}


def _live_agent(runner: Any, ctx: CommandContext) -> tuple[str | None, Any]:
    """Return ``(session_id, agent)`` for this conversation's live session.

    ``agent`` is ``None`` when no session id is routed yet or the session is
    not resident in the manager (parked transcripts load on the next run).
    """
    sid = runner.session_store.get(ctx.source)
    if not sid:
        return None, None
    router = getattr(runner, "router", None)
    manager = getattr(router, "session_manager", None) if router is not None else None
    if manager is None:
        return sid, None
    info = manager.get_session(sid)
    return sid, (getattr(info, "agent", None) if info is not None else None)


async def _cmd_approve(ctx: CommandContext) -> dict[str, Any]:
    """``/approve``: allow the pending tool-permission request.

    ``/approve always`` additionally remembers the verdict for this session
    (same semantics as the desktop card's always-allow).  Answered even when
    nothing is pending so the user learns the request already timed out.
    """
    _sid, agent = _live_agent(ctx.runner, ctx)
    loop = getattr(agent, "loop", None) if agent is not None else None
    if loop is None or not getattr(loop, "permission_pending", False):
        await ctx.adapter.send(
            ctx.source.chat_id,
            "Nothing is pending approval right now.",
        )
        return {"decision": "handled"}
    remember = ctx.args.strip().lower() in ("always", "remember")
    agent.respond_permission(True, remember)
    await ctx.adapter.send(
        ctx.source.chat_id,
        "✅ Approved and remembered for this session."
        if remember else "✅ Approved. Continuing.",
    )
    return {"decision": "handled"}


async def _cmd_deny(ctx: CommandContext) -> dict[str, Any]:
    """``/deny``: refuse the pending tool-permission request."""
    _sid, agent = _live_agent(ctx.runner, ctx)
    loop = getattr(agent, "loop", None) if agent is not None else None
    if loop is None or not getattr(loop, "permission_pending", False):
        await ctx.adapter.send(
            ctx.source.chat_id,
            "Nothing is pending approval right now.",
        )
        return {"decision": "handled"}
    agent.respond_permission(False)
    await ctx.adapter.send(
        ctx.source.chat_id,
        "❌ Denied. The agent will take another route.",
    )
    return {"decision": "handled"}


async def _cmd_stop(ctx: CommandContext) -> dict[str, Any]:
    """``/stop``: cancel the running turn for this conversation.

    Rides the router's cancel channel (loop cancel-event + task teardown),
    the same one the desktop stop button uses.  Only effective on adapters
    that dispatch inbound messages concurrently; a strictly sequential
    adapter processes this command only after the current turn ends.
    """
    sid = ctx.runner.session_store.get(ctx.source)
    router = getattr(ctx.runner, "router", None)
    cancelled = bool(router.cancel_session(sid)) if router is not None and sid else False
    if cancelled:
        await ctx.adapter.send(
            ctx.source.chat_id,
            "✅ Stopped. Send a message to continue.",
        )
    else:
        await ctx.adapter.send(
            ctx.source.chat_id,
            "Nothing is running in this conversation.",
        )
    return {"decision": "handled"}


async def _cmd_compact(ctx: CommandContext) -> dict[str, Any]:
    """``/compact``: manually compact this conversation's context now."""
    from encre.server.session_manager import SessionState

    sid = ctx.runner.session_store.get(ctx.source)
    router = getattr(ctx.runner, "router", None)
    manager = getattr(router, "session_manager", None) if router is not None else None
    info = manager.get_session(sid) if manager is not None and sid else None
    if info is None:
        await ctx.adapter.send(
            ctx.source.chat_id,
            "No loaded session to compact yet; send a message first.",
        )
        return {"decision": "handled"}
    if info.state != SessionState.IDLE:
        await ctx.adapter.send(
            ctx.source.chat_id,
            "The agent is busy; wait for the turn to end or send /stop first.",
        )
        return {"decision": "handled"}
    loop = getattr(info.agent, "loop", None)
    result = await loop.force_compact() if loop is not None else None
    if result:
        await ctx.adapter.send(
            ctx.source.chat_id,
            f"✅ Context compacted: {result['old_tokens'] // 1000}k -> "
            f"{result['new_tokens'] // 1000}k tokens "
            f"({result['old_count']} -> {result['new_count']} messages).",
        )
    else:
        await ctx.adapter.send(
            ctx.source.chat_id,
            "Nothing to compact -- the context is below the compaction threshold.",
        )
    return {"decision": "handled"}


async def _cmd_status(ctx: CommandContext) -> dict[str, Any]:
    """``/status``: one-message snapshot of this conversation."""
    from encre.utils.tokens import count_message_tokens

    store = ctx.runner.session_store
    sid = store.get(ctx.source)
    mode = store.get_active_mode(ctx.session_key) or ""
    pin = store.get_preferred_model(ctx.session_key)
    lines = ["Status:"]
    lines.append(f"Session: {sid[:8] if sid else 'none yet'}")
    lines.append(f"Mode: {mode or 'normal'}")
    lines.append(f"Model: {pin or 'auto'}")
    _sid, agent = _live_agent(ctx.runner, ctx)
    loop = getattr(agent, "loop", None) if agent is not None else None
    if loop is not None:
        live_mode = getattr(loop.config, "slash_command_mode", "") or mode
        if live_mode != mode:
            lines[2] = f"Mode: {live_mode or 'normal'}"
        model = getattr(loop.config, "model", "") or ""
        if model:
            lines[3] = f"Model: {model}"
        session = getattr(agent, "session", None)
        if session is not None:
            ctx_msgs = session.get_context_messages()
            backend = getattr(loop, "backend", None)
            window = backend.context_window_size() if backend else 0
            lines.append(
                f"Context: {count_message_tokens(ctx_msgs) // 1000}k / "
                f"{window // 1000}k tokens ({len(ctx_msgs)} messages)"
            )
            ws = (session.metadata or {}).get("workspace", "")
            lines.append(f"Workspace: {ws or '-'}")
    else:
        lines.append("Session details load after the next message.")
    await ctx.adapter.send(ctx.source.chat_id, "\n\n".join(lines))
    return {"decision": "handled"}


def build_command_registry() -> GatewayCommandRegistry:
    """Create the registry seeded with the built-in gateway commands.

    New commands should be registered here (or via a later ``register`` call)
    rather than by editing dispatch logic, which is why the built-ins are
    plain data + functions.

    Returns:
        A registry containing ``help``, ``new``, ``model``, ``plan``,
        ``spec``, ``init``, ``sessions``, ``resume``, ``approve``,
        ``deny``, ``stop``, ``compact`` and ``status``.
    """
    registry = GatewayCommandRegistry()

    async def _cmd_help(ctx: CommandContext) -> dict[str, Any]:
        """``/help``: list every registered command with its usage.

        The registry itself is the source of truth, so newly registered
        commands appear here automatically without touching this handler.

        Args:
            ctx: The resolved command context.

        Returns:
            ``{"decision": "handled"}`` -- always answered in-band.
        """
        lines = ["Available commands:"]
        for action in registry.list_actions():
            hint = f" {action.argument_hint}" if action.argument_hint else ""
            lines.append(f"/{action.name}{hint} — {action.description}")
        lines.append("Send a command by itself for details; unknown /commands go to the agent.")
        lines.append("Commands defined in settings or project *.md files work here too.")
        # Blank lines between entries: some IM clients (e.g. WeChat iLink)
        # render single newlines unreliably, while paragraph breaks always
        # survive -- and the airy spacing reads better in mobile bubbles.
        await ctx.adapter.send(ctx.source.chat_id, "\n\n".join(lines))
        return {"decision": "handled"}

    registry.register(CommandAction(
        name="help",
        description="Show available commands",
        handler=_cmd_help,
    ))
    registry.register(CommandAction(
        name="new",
        description="Start a fresh conversation",
        handler=_cmd_new,
    ))
    registry.register(CommandAction(
        name="model",
        description="Show or set the model for this conversation",
        handler=_cmd_model,
        argument_hint="<model-id>",
    ))
    registry.register(CommandAction(
        name="plan",
        description="Plan mode: plan first, run after you confirm",
        handler=lambda ctx: _cmd_set_mode(ctx, "plan", "Plan"),
    ))
    registry.register(CommandAction(
        name="spec",
        description="Spec mode: write a spec before implementing",
        handler=lambda ctx: _cmd_set_mode(ctx, "spec", "Spec"),
    ))
    registry.register(CommandAction(
        name="init",
        description="Analyze the project and generate workspace configuration",
        handler=_cmd_init,
    ))
    registry.register(CommandAction(
        name="sessions",
        description="List this conversation's sessions with their ids",
        handler=_cmd_sessions,
    ))
    registry.register(CommandAction(
        name="resume",
        description="Switch this conversation to one of its own sessions",
        handler=_cmd_resume,
        argument_hint="<id>",
    ))
    registry.register(CommandAction(
        name="approve",
        description="Approve the pending tool-permission request (/approve always to remember)",
        handler=_cmd_approve,
        argument_hint="[always]",
    ))
    registry.register(CommandAction(
        name="deny",
        description="Refuse the pending tool-permission request",
        handler=_cmd_deny,
    ))
    registry.register(CommandAction(
        name="stop",
        description="Cancel the running turn",
        handler=_cmd_stop,
    ))
    registry.register(CommandAction(
        name="compact",
        description="Compact this conversation's context now",
        handler=_cmd_compact,
    ))
    registry.register(CommandAction(
        name="status",
        description="Show session id, mode, model, context and workspace",
        handler=_cmd_status,
    ))
    return registry


def register_command_hook(runner: Any, registry: GatewayCommandRegistry) -> None:
    """Wire the registry into the process-wide hook registry.

    A single wildcard handler is enough: it resolves the typed name per event
    and answers only known commands.  The returned closure keeps a reference
    to *runner* so handlers can reach the session store and adapters without
    globals.

    Args:
        runner: The gateway runner whose adapters emit command events.
        registry: The action registry built by :func:`build_command_registry`.
    """

    async def _on_command(_event_type: str, context: dict[str, Any]) -> dict[str, Any] | None:
        return await dispatch_command(context, runner, registry)

    get_hook_registry().register(COMMAND_WILDCARD, _on_command)
