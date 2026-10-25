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

"""Plugin host capability object.

The :class:`PluginHost` is the *missing* piece that makes
:class:`~encre.plugins.context.PluginContext` surfaces actually work.
Without a host, ``activate_all()`` passes ``host=None`` and every
``context.*.register(...)`` call degrades to a no-op.

The host implements every ``plugin_register_*`` method that the context
API classes call.  Each method stores the registered item in an internal
registry and returns an undo closure.  When a real registry is supplied
(tool registry, skill registry, hook system, ...), registrations are
also forwarded so the agent loop picks them up immediately.

Three additional surfaces beyond the original 12 let plugins reach into
server-level concerns (WS dispatch, HTTP admin routes, lifecycle hooks).
"""

import asyncio
import contextlib
import copy
import json
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from encre.logging_config import get_logger
from encre.plugins.guard import SENTINEL, absorb, absorb_async, is_fatal

logger = get_logger("encre.plugins.host")

_UNDO = Callable[[], None]

# Upper bound on messages all plugin context providers may inject into a
# single turn — one runaway provider must not flood the model context.
_MAX_PLUGIN_CONTEXT_MESSAGES = 50


def _noop_undo() -> None:
    """Sentinel undo returned when no real registry is attached."""


async def _invoke_maybe_coro(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Invoke *fn*; await the result when the callback returned a coroutine."""
    result = fn(*args, **kwargs)
    return await result if hasattr(result, "__await__") else result


class PluginHost:
    """Concrete host capability provider for :class:`PluginContext`.

    Construct with whichever real registries the embedding application
    owns.  Pass the result of :meth:`build_host_dict` to
    :meth:`~encre.plugins.registry.PluginRegistry.activate_all` so every
    plugin's ``on_activate(context)`` can actually register things.

    After activation, read the accumulators (``.commands``,
    ``.ui_providers``, ``.gateway_adapters``, ...) to wire plugin
    contributions into the server, the frontend, or the gateway.
    """

    def __init__(
        self,
        *,
        tool_registry: Any = None,
        hook_system: Any = None,
        backend_registry: Any = None,
        permission_registry: Any = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._hook_system = hook_system
        self._backend_registry = backend_registry
        self._permission_registry = permission_registry

        # Internal accumulators (always populated, even without real registries).
        self._tools: list[Any] = []
        self._backends: dict[str, type] = {}
        self._commands: dict[str, dict[str, Any]] = {}
        self._sub_agents: dict[str, dict[str, Any]] = {}
        self._templates: dict[str, str] = {}
        # Shared-name ownership.  Commands/backends/sub-agents/templates are
        # single-name registries; without owners a later plugin silently
        # replaced an earlier one and its undo could delete the winner.
        self._command_owners: dict[str, str] = {}
        self._backend_owners: dict[str, str] = {}
        self._sub_agent_owners: dict[str, str] = {}
        self._template_owners: dict[str, str] = {}
        self._context_providers: list[Any] = []
        self._ui_providers: list[Any] = []
        self._contributions: list[dict[str, Any]] = []
        self._tool_meta_entries: list[dict[str, Any]] = []
        self._gateway_adapters: list[Any] = []
        self._memory_stores: list[Any] = []
        self._system_prompt_sections: list[dict[str, Any]] = []

        # Simple event bus for plugin events.
        self._event_subs: dict[str, list[tuple[str, Callable[..., Any]]]] = defaultdict(list)
        # Phase handlers registered while NO hook system existed (the shared
        # registry host outlives agents).  Explicit staging table — distinct
        # from ``_event_subs`` so whitelist-rejected handlers (which stay in
        # ``_event_subs`` as an inert fallback, by design) can never be
        # mistaken for replayable ones.  ``replay_phase_handlers_into``
        # consumes this table (idempotently per hook system).
        self._staged_phase_handlers: dict[str, list[tuple[str, Callable[..., Any]]]] = defaultdict(list)
        # Arbitration metadata for phase handlers (priority/timeout/source),
        # keyed by id(handler); lets replay carry the admission options into
        # a real hook system.
        self._phase_opts: dict[int, dict[str, Any]] = {}
        # handler ids returned by the hook system, keyed by (phase,
        # id(handler)); lets off() unregister the exact registration.
        self._phase_handler_ids: dict[tuple[str, int], str] = {}
        # Memo for replay_phase_handlers_into(): id(hook_system) -> set of
        # id(handler) already replayed there (idempotency across repeated
        # agent loads).
        self._replayed_phase_handlers: dict[int, set[int]] = {}

        # New surfaces (Phase A+): server integration.
        self._ws_handlers: dict[str, Callable[..., Any]] = {}
        # Per-plugin WS handlers.  A plugin owns its own message-type
        # namespace: two plugins may register the same type without one
        # silently replacing the other.  The flat map above stays as the
        # legacy fallback for frames that do not name a plugin.
        self._ws_handlers_by_plugin: dict[str, dict[str, Callable[..., Any]]] = {}
        self._http_routes: list[dict[str, Any]] = []
        self._lifecycle_hooks: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self._ws_broadcaster: Any = None
        # Plugin llm_call gateway: an async callable
        # ``(messages, *, max_tokens=None, system=None) -> str`` injected by
        # the loop so plugin ``resources.llm_chat`` uses the live backend
        # (including fallback swaps).  None until the first model phase.
        self.llm_gateway: Any = None

    # ── host dict ───────────────────────────────────────────────

    def build_host_dict(self) -> dict[str, Any]:
        """Return the surface→host mapping for :meth:`build_context`."""
        return {
            "tools": self,
            "backends": self,
            "loop": self,
            "events": self,
            "permissions": self,
            "commands": self,
            "sub_agents": self,
            "templates": self,
            "context_providers": self,
            "ui_providers": self,
            "ui_contributions": self,
            "gateway_adapters": self,
            "memory_stores": self,
            "ws_handlers": self,
            "http_routes": self,
            "lifecycle": self,
            "llm": self,
        }

    # ── tools surface ───────────────────────────────────────────

    def plugin_register_tool(self, tool: Any, source: str = "") -> _UNDO:
        """Register *tool*; returns undo closure."""
        self._tools.append(tool)
        reg = self._tool_registry
        if reg is not None:
            absorb(
                reg.register,
                tool,
                context=f"tool_registry.register for {getattr(tool, 'name', '?')}",
                log="debug",
            )
        # Record ownership in the engine: a deactivated plugin's tools are
        # denied at execution time (zero-window, before every other layer).
        tool_name = str(getattr(tool, "name", "") or "")
        if tool_name and source:
            from encre import native as _native

            absorb(
                _native.permission_register_plugin_tool,
                tool_name,
                source,
                context=f"engine tool ownership for {tool_name!r}",
                log="debug",
            )
        return lambda: self._remove_tool(tool)

    def register_tool(self, tool: Any) -> _UNDO:
        """Legacy fallback for old-style registries."""
        return self.plugin_register_tool(tool)

    def _remove_tool(self, tool: Any) -> None:
        with contextlib.suppress(Exception):
            self._tools.remove(tool)
        reg = self._tool_registry
        if reg is not None:
            name = str(getattr(tool, "name", "") or "")
            if not name:
                return
            # Only remove the live entry when it is still this exact object:
            # another plugin may have won a name conflict in the meantime.
            current = None
            with contextlib.suppress(Exception):
                current = reg.get(name)
            if current is tool:
                with contextlib.suppress(Exception):
                    reg.unregister(name)

    # ── backends surface ────────────────────────────────────────

    def plugin_register_backend(self, name: str, backend_class: type, source: str = "") -> _UNDO:
        """Register backend class under *name*; returns undo.

        First owner wins.  A different plugin cannot silently replace an
        existing backend; it gets a warning and a no-op undo.
        """
        owner = self._backend_owners.get(name)
        if owner and owner != source:
            logger.warning("Backend %r already owned by %r; %r ignored", name, owner, source)
            return _noop_undo
        self._backends[name] = backend_class
        self._backend_owners[name] = source
        if self._backend_registry is not None:
            absorb(
                self._backend_registry.__setitem__,
                name,
                backend_class,
                context=f"backend_registry set for {name!r}",
                log="debug",
            )

        def _undo() -> None:
            if self._backend_owners.get(name) != source:
                return
            self._backends.pop(name, None)
            self._backend_owners.pop(name, None)

        return _undo

    # ── loop surface ────────────────────────────────────────────

    def on(
        self,
        phase: str,
        handler: Callable[..., Any],
        *,
        source: str = "",
        priority: int = 100,
        timeout: float = 5.0,
    ) -> None:
        """Register *handler* as a phase hook with admission metadata.

        Args:
            phase: The lifecycle phase to intercept.
            handler: The plugin handler callable.
            source: Plugin name (``""`` for host-internal registrations).
            priority: Lower runs earlier (default ``100``); carried through to
                the hook system's arbitration layer.
            timeout: Per-invocation wall-clock budget in seconds; carried
                through to the hook system's arbitration layer.
        """
        opts = {
            "priority": int(priority),
            "timeout": max(0.1, float(timeout)),
            "source": str(source),
        }
        self._phase_opts[id(handler)] = opts
        hs = self._hook_system
        if hs is not None:
            result = absorb(
                hs.register_handler,
                phase,
                handler,
                metadata=dict(opts),
                default=SENTINEL,
                context=f"hook_system.register_handler for {phase!r}",
                log="debug",
            )
            if result is not SENTINEL:
                self._phase_handler_ids[(phase, id(handler))] = str(result)
                return
            # The hook system rejected the event name (ValueError for a
            # phase outside its whitelist).  Log loudly instead of letting
            # the plugin believe its interception is active.  The handler
            # stays in ``_event_subs`` as an inert fallback only — it is
            # deliberately NOT staged for replay.
            logger.warning(
                "Plugin phase handler for {!r} was rejected by the hook system (unknown event); kept in host fallback only",
                phase,
            )
            self._event_subs[phase].append((str(source), handler))
            return
        # No hook system yet (the shared registry host outlives agents):
        # track locally (``_event_subs`` keeps the host/tests-facing view)
        # AND stage explicitly so the next ``replay_phase_handlers_into``
        # installs this handler — for host-internal AND plugin sources.
        self._staged_phase_handlers[phase].append((str(source), handler))
        self._event_subs[phase].append((str(source), handler))

    def replay_phase_handlers_into(self, hook_system: Any) -> int:
        """Replay staged phase handlers into a *real* hook system.

        Handlers registered while this host had no hook system (the shared
        registry host exists before any agent) sit in
        ``_staged_phase_handlers`` and would never run.  The agent replays
        them into its real hook system at load time, carrying the admission
        metadata recorded at :meth:`on` time.  Both host-internal and
        plugin-sourced handlers are replayed.  Idempotent per
        ``(hook_system, handler)`` pair (memoised), so a repeated
        ``_load_plugins`` never double-registers, while a *new* hook system
        (agent reload) correctly receives the handlers again.  Handlers
        whose registration is rejected on replay (whitelist changed) are
        reported loudly and stay staged.

        Returns the number of handlers replayed.
        """
        memo = self._replayed_phase_handlers.setdefault(id(hook_system), set())
        replayed = 0
        for phase in list(self._staged_phase_handlers):
            for source, handler in list(self._staged_phase_handlers[phase]):
                if id(handler) in memo:
                    continue
                opts = self._phase_opts.get(id(handler))
                result = absorb(
                    hook_system.register_handler,
                    phase,
                    handler,
                    metadata=dict(opts) if opts else None,
                    default=SENTINEL,
                    context=f"phase handler replay for {phase!r} (from {source!r})",
                )
                if result is not SENTINEL:
                    memo.add(id(handler))
                    self._phase_handler_ids[(phase, id(handler))] = str(result)
                    replayed += 1
                else:
                    logger.warning(
                        "Staged phase handler for {!r} (from {!r}) was rejected on replay; kept staged",
                        phase,
                        source,
                    )
        return replayed

    async def collect_context_messages(
        self,
        context_state: dict[str, Any] | None = None,
        *,
        timeout: float = 5.0,
    ) -> list[dict[str, Any]]:
        """Collect context messages from every registered plugin provider.

        Providers are COMPLEMENTARY to the built-in context assembly, not a
        replacement for it: each provider exposes ``provide(context_state)``
        (sync or async) and contributes extra message dicts that the loop
        merges into the per-turn request the built-in pipeline produced.
        Providers may carry ``name`` / ``priority`` (lower runs earlier).
        Every call is guarded and timeout-bounded so a hanging or crashing
        provider can never stall or break the turn — a failure simply
        contributes no messages.  Non-dict results are dropped; the total
        is capped so one provider cannot flood the model context.

        Providers run CONCURRENTLY: the serial chain used to add one slow
        provider's full timeout to every other provider's latency on each
        model call.  ``asyncio.gather`` preserves the priority order, so
        the merged output is byte-for-byte the same as the serial version.
        """
        entries = list(self._context_providers)
        if not entries:
            return []

        def _priority(p: Any) -> float:
            try:
                return float(getattr(p, "priority", 100.0))
            except (TypeError, ValueError):
                return 100.0

        pairs = [(p, getattr(p, "provide", None)) for p in sorted(entries, key=_priority)]
        pairs = [(p, provide) for p, provide in pairs if callable(provide)]
        if not pairs:
            return []

        async def _collect(provider: Any, provide: Callable[..., Any]) -> list[Any]:
            # Full isolation wrap: ``provide(...)`` may raise *synchronously*
            # before producing an awaitable (absorb_async never sees it), so
            # the guard here keeps a crashing provider from failing the
            # whole gather — which would drop every other provider's
            # messages for the turn.
            try:
                result = await absorb_async(
                    _invoke_maybe_coro(provide, dict(context_state or {})),
                    timeout=timeout,
                    default=[],
                    context=f"context provider {getattr(provider, 'name', '?')!r}",
                )
            except BaseException as exc:
                if is_fatal(exc):
                    raise
                return []
            return result if isinstance(result, list) else []

        results = await asyncio.gather(*(_collect(p, provide) for p, provide in pairs))
        messages: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, list):
                messages.extend(m for m in result if isinstance(m, dict))
        return messages[:_MAX_PLUGIN_CONTEXT_MESSAGES]

    async def collect_memory_fragments(
        self,
        query: str,
        *,
        top_k: int = 3,
        timeout: float = 5.0,
    ) -> list[Any]:
        """Collect recall fragments from registered plugin memory stores.

        Each store exposes ``search(query, top_k)`` (sync or async) and
        returns fragment-like dicts or objects.  Every call is guarded and
        timeout-bounded — a hanging or crashing store simply contributes
        no fragments and recall degrades to the built-in index.

        Stores run CONCURRENTLY (order preserved by ``asyncio.gather``) so
        one slow store's timeout no longer stacks onto every other store's
        recall latency.
        """
        stores = list(self._memory_stores)
        if not stores:
            return []
        pairs = [(s, getattr(s, "search", None)) for s in stores]
        pairs = [(s, search) for s, search in pairs if callable(search)]
        if not pairs:
            return []

        async def _search(store: Any, search: Callable[..., Any]) -> list[Any]:
            # Same isolation wrap as ``_collect``: a synchronous raise from
            # ``search(...)`` must not fail the whole gather.
            try:
                result = await absorb_async(
                    _invoke_maybe_coro(search, query, top_k),
                    timeout=timeout,
                    default=[],
                    context=f"memory store {getattr(store, 'name', '?')!r}",
                )
            except BaseException as exc:
                if is_fatal(exc):
                    raise
                return []
            return result if isinstance(result, list) else []

        results = await asyncio.gather(*(_search(s, search) for s, search in pairs))
        fragments: list[Any] = []
        for result in results:
            if isinstance(result, list):
                fragments.extend(result)
        return fragments[:top_k]

    async def collect_permission_votes(
        self,
        tool_name: str,
        *,
        timeout: float = 3.0,  # noqa: ARG002 - call-site compat; engine is sync
    ) -> list[bool]:
        """Return the plugin-policy verdict for *tool_name* from the engine.

        The vote is computed inside the Rust permission engine (deny beats
        allow, exact rule beats wildcard, per-plugin abstention) -- this is
        a thin forwarding shell with no Python voting logic.  ``timeout`` is
        kept for call-site compatibility; the engine answers synchronously.
        """
        from encre import native as _native
        vote = _native.permission_plugin_vote(tool_name)
        return [bool(vote)] if vote is not None else []

    def off(self, phase: str, handler: Callable[..., Any]) -> None:
        """Remove *handler* from *phase*."""
        hs = self._hook_system
        if hs is not None:
            hid = self._phase_handler_ids.pop((phase, id(handler)), None)
            if hid is not None:
                with contextlib.suppress(Exception):
                    hs.unregister_handler(hid)
        self._phase_opts.pop(id(handler), None)
        subs = self._event_subs.get(phase, [])
        subs = [s for s in subs if s[1] is not handler]
        if subs:
            self._event_subs[phase] = subs
        else:
            self._event_subs.pop(phase, None)
        # Drop any pending staging too, so a deactivated plugin's handler
        # can never be replayed into a hook system created later (agent
        # reload).
        staged = self._staged_phase_handlers.get(phase, [])
        staged = [s for s in staged if s[1] is not handler]
        if staged:
            self._staged_phase_handlers[phase] = staged
        else:
            self._staged_phase_handlers.pop(phase, None)

    # ── events surface ──────────────────────────────────────────

    def subscribe(self, event_type: str, handler: Callable[..., Any], source: str = "") -> _UNDO:
        """Subscribe to *event_type*; returns unsubscribe closure."""
        self._event_subs[event_type].append((source, handler))
        return lambda: self._unsubscribe(event_type, source, handler)

    def emit(self, event_type: str, payload: dict[str, Any] | None = None, source: str = "") -> None:
        """Emit a plugin event (best effort, per-subscriber isolated copy).

        Every handler receives its own deep copy with a trusted
        ``__source__`` entry, so one subscriber cannot mutate what the next
        sees and the emitting plugin's identity is not forgeable.
        """
        payload = payload or {}
        for src, handler in list(self._event_subs.get(event_type, [])):
            try:
                detail = copy.deepcopy(payload)
                detail["__source__"] = source
            except Exception:
                detail = {"__source__": source}
            absorb(
                handler,
                detail,
                context=f"event handler for {event_type!r} (from {src!r})",
                log="debug",
            )

    def _unsubscribe(self, event_type: str, source: str, handler: Callable[..., Any]) -> None:
        subs = self._event_subs.get(event_type, [])
        subs = [s for s in subs if not (s[0] == source and s[1] is handler)]
        if subs:
            self._event_subs[event_type] = subs
        else:
            self._event_subs.pop(event_type, None)

    # ── permissions surface ─────────────────────────────────────

    def plugin_register_policy(self, policy: Any, source: str = "") -> _UNDO:
        """Register declarative permission rules; returns undo.

        ``policy`` is a mapping ``{tool_name | "prefix*" | "*": "allow" |
        "deny"}`` (an object exposing the same mapping as a ``.rules``
        attribute is also accepted).  The rules are pushed straight into the
        Rust permission engine -- Python keeps no voting logic, so the
        verdict cannot be bypassed by manipulating Python-side state.
        Callback-style policies (a ``decide()`` callable) are rejected:
        static rules are the only supported shape.
        """
        rules = getattr(policy, "rules", policy)
        if not isinstance(rules, dict) or not rules:
            raise TypeError(
                "permission policy must be a mapping of "
                "{tool_name | '*': 'allow' | 'deny'}; callback policies "
                "(decide-callables) are no longer accepted -- rules live "
                "in the Rust permission engine"
            )
        from encre import native as _native
        payload = json.dumps(
            {str(k): str(v).strip().lower() for k, v in rules.items()},
        )
        _native.permission_add_plugin_policy(source, payload)
        return lambda: self._remove_plugin_policies(source)

    def _remove_plugin_policies(self, source: str) -> None:
        from encre import native as _native
        _native.permission_remove_plugin_policies(source)

    # ── commands surface ────────────────────────────────────────

    def plugin_register_command(self, command: dict[str, Any], source: str = "") -> _UNDO:
        """Register a slash-command definition; first owner wins."""
        name = command.get("name", "")
        if not name:
            return _noop_undo
        owner = self._command_owners.get(name)
        if owner and owner != source:
            logger.warning("Command %r already owned by %r; %r ignored", name, owner, source)
            return _noop_undo
        entry = dict(command)
        # Keep the contributing plugin on the entry so the config frame
        # can attribute the command (renderer shows its origin).
        entry["plugin"] = source
        self._commands[name] = entry
        self._command_owners[name] = source

        def _undo() -> None:
            if self._command_owners.get(name) != source:
                return
            self._commands.pop(name, None)
            self._command_owners.pop(name, None)

        return _undo

    # ── sub_agents surface ──────────────────────────────────────

    def plugin_register_sub_agent(self, definition: dict[str, Any], source: str = "") -> _UNDO:
        """Register a sub-agent definition; first owner wins."""
        name = definition.get("name", "")
        if not name:
            return _noop_undo
        owner = self._sub_agent_owners.get(name)
        if owner and owner != source:
            logger.warning("Sub-agent %r already owned by %r; %r ignored", name, owner, source)
            return _noop_undo
        self._sub_agents[name] = definition
        self._sub_agent_owners[name] = source

        def _undo() -> None:
            if self._sub_agent_owners.get(name) != source:
                return
            self._sub_agents.pop(name, None)
            self._sub_agent_owners.pop(name, None)

        return _undo

    # ── templates surface ───────────────────────────────────────

    def plugin_register_template(self, name: str, template: str, source: str = "") -> _UNDO:
        """Register prompt template under *name*; first owner wins."""
        owner = self._template_owners.get(name)
        if owner and owner != source:
            logger.warning("Template %r already owned by %r; %r ignored", name, owner, source)
            return _noop_undo
        self._templates[name] = template
        self._template_owners[name] = source

        def _undo() -> None:
            if self._template_owners.get(name) != source:
                return
            self._templates.pop(name, None)
            self._template_owners.pop(name, None)

        return _undo

    # ── context_providers surface ───────────────────────────────

    def plugin_register_context_provider(self, provider: Any, source: str = "") -> _UNDO:  # noqa: ARG002
        """Register a context *provider*; returns undo."""
        self._context_providers.append(provider)
        return lambda: self._remove_item(self._context_providers, provider)

    # ── system_prompt_sections surface ──────────────────────────

    def plugin_register_system_prompt(self, section: Any, source: str = "") -> _UNDO:
        """Register a system-prompt *section* contribution; returns undo.

        Sections are COMPLEMENTARY to the built-in prompt body, never a
        replacement: the built-in builder produces the core identity,
        rules and tool guidance, and plugin sections are merged around
        that body (sorted by ``priority``, lower first).  A section is any
        object exposing ``content: str`` and an optional ``priority:
        float``; a plain string is also accepted.
        """
        entry = {"section": section, "source": source}
        self._system_prompt_sections.append(entry)
        return lambda: self._remove_item(self._system_prompt_sections, entry)

    def collect_system_prompt_sections(self) -> list[str]:
        """Collect plugin system-prompt section texts, priority-sorted.

        Failures and empty sections are skipped -- a broken plugin can
        never blank or corrupt the shared system prompt.
        """

        def _prio(entry: dict[str, Any]) -> float:
            try:
                return float(getattr(entry["section"], "priority", 100.0))
            except (TypeError, ValueError):
                return 100.0

        out: list[str] = []
        for entry in sorted(self._system_prompt_sections, key=_prio):
            section = entry["section"]
            content = getattr(section, "content", None)
            if not isinstance(content, str):
                content = section if isinstance(section, str) else ""
            if content.strip():
                out.append(content.strip())
        return out

    # ── ui_providers surface ────────────────────────────────────

    def plugin_register_ui_provider(self, provider: Any, source: str = "") -> _UNDO:
        """Register a UI *provider*; returns undo.

        Each provider should have ``name``, ``entry_point`` (JS module
        path), and ``mount_point`` (sidebar/panel/settings/chat-overlay).
        """
        entry = {"provider": provider, "source": source}
        self._ui_providers.append(entry)
        return lambda: self._remove_item(self._ui_providers, entry)

    # ── ui_contributions surface ────────────────────────────────

    _CONTRIBUTION_KINDS = frozenset({"theme", "settings", "context_menu", "search_mode", "ui_override", "js_module"})

    def plugin_register_contribution(self, kind: str, spec: Any, source: str = "") -> _UNDO:
        """Register a declarative UI *contribution*; returns undo.

        *kind* is one of ``theme`` / ``settings`` / ``context_menu`` /
        ``search_mode``.  *spec* must be a JSON-safe dict — the desktop
        renderer renders it into an existing first-party surface (see
        ``features/plugin_contributions.ts``), so plugins never ship markup.
        """
        if kind not in self._CONTRIBUTION_KINDS:
            logger.warning("plugin_register_contribution: unknown kind %r from %r", kind, source)
            return lambda: None
        entry = {"kind": kind, "spec": spec, "source": source}
        self._contributions.append(entry)
        return lambda: self._remove_item(self._contributions, entry)

    # ── gateway_adapters surface ────────────────────────────────

    def plugin_register_gateway_adapter(self, adapter: Any, source: str = "") -> _UNDO:  # noqa: ARG002
        """Register a gateway *adapter*; returns undo."""
        self._gateway_adapters.append(adapter)
        return lambda: self._remove_item(self._gateway_adapters, adapter)

    # ── memory_stores surface ───────────────────────────────────

    def plugin_register_memory_store(self, store: Any, source: str = "") -> _UNDO:  # noqa: ARG002
        """Register a memory *store*; returns undo."""
        self._memory_stores.append(store)
        return lambda: self._remove_item(self._memory_stores, store)

    # ── ws_handlers surface (new) ───────────────────────────────

    def plugin_register_ws_handler(self, message_type: str, handler: Callable[..., Any], source: str = "") -> _UNDO:
        """Register a WS message handler for *message_type*; returns undo.

        Handlers are namespaced by the owning plugin.  A frame's ``plugin``
        field selects the owning namespace, so plugin A can never receive
        plugin B's action and one plugin's registration cannot clobber
        another's.  Frames without a plugin field keep the legacy flat
        lookup for backwards compatibility.
        """
        if not source:
            self._ws_handlers[message_type] = handler
            return lambda: self._ws_handlers.pop(message_type, None)

        bucket = self._ws_handlers_by_plugin.setdefault(source, {})
        bucket[message_type] = handler
        # Legacy fallback: first registration wins, never a cross-plugin
        # overwrite of an already-owned type.
        self._ws_handlers.setdefault(message_type, handler)

        def _undo() -> None:
            cur = self._ws_handlers_by_plugin.get(source)
            if cur is not None and cur.get(message_type) is handler:
                cur.pop(message_type, None)
                if not cur:
                    self._ws_handlers_by_plugin.pop(source, None)
            if self._ws_handlers.get(message_type) is handler:
                self._ws_handlers.pop(message_type, None)

        return _undo

    # ── http_routes surface (new) ───────────────────────────────

    def plugin_register_http_route(self, method: str, path: str, handler: Callable[..., Any], source: str = "") -> _UNDO:
        """Register an HTTP admin route; returns undo.

        *path* supports prefix matching (e.g. ``/plugin/foo/`` matches
        ``/plugin/foo/bar``).  *handler* receives ``(path, query, body, headers)``
        and returns ``(status, body, headers)``.
        """
        route = {"method": method.upper(), "path": path, "handler": handler, "source": source}
        self._http_routes.append(route)
        return lambda: self._remove_item(self._http_routes, route)

    # ── lifecycle surface (new) ─────────────────────────────────

    def plugin_register_lifecycle_hook(self, event: str, handler: Callable[..., Any], source: str = "") -> _UNDO:  # noqa: ARG002
        """Register a lifecycle *handler* for *event* (startup/shutdown).

        Returns undo closure.
        """
        self._lifecycle_hooks[event].append(handler)
        return lambda: self._remove_item(self._lifecycle_hooks[event], handler)

    async def fire_lifecycle(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Fire lifecycle *event* hooks (async, best-effort)."""
        for handler in list(self._lifecycle_hooks.get(event, [])):
            await absorb_async(
                _invoke_maybe_coro(handler, *args, **kwargs),
                context=f"lifecycle hook for {event!r}",
            )

    async def broadcast(self, event_type: str, payload: dict[str, Any]) -> None:
        """Broadcast a WS event to all connected frontend clients.

        Used by plugins to surface UI-side effects (notifications,
        tool results, etc.) without reaching into the transport layer.
        """
        broadcaster = self._ws_broadcaster
        if broadcaster is None:
            logger.warning("broadcast: no _ws_broadcaster set")
            return
        try:
            await broadcaster.broadcast_event(event_type, payload)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError | KeyboardInterrupt):
                raise
            logger.warning("broadcast %s failed: %s", event_type, exc)

    # ── accumulators (read after activation) ───────────────────

    @property
    def tools(self) -> list[Any]:
        return list(self._tools)

    @property
    def backends(self) -> dict[str, type]:
        return dict(self._backends)

    @property
    def policies(self) -> dict[str, dict[str, str]]:
        """Declarative plugin permission rules live in the Rust engine.

        Returns a snapshot ``{"<plugin>": {"<tool|*>": "allow" | "deny"}}``
        read straight from the engine table.
        """
        from encre import native as _native
        raw = _native.permission_get_plugin_policies()
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}

    @property
    def commands(self) -> dict[str, dict[str, Any]]:
        return dict(self._commands)

    @property
    def sub_agents(self) -> dict[str, dict[str, Any]]:
        return dict(self._sub_agents)

    @property
    def templates(self) -> dict[str, str]:
        return dict(self._templates)

    @property
    def context_providers(self) -> list[Any]:
        return list(self._context_providers)

    @property
    def ui_providers(self) -> list[Any]:
        return [entry["provider"] for entry in self._ui_providers]

    @property
    def ui_provider_entries(self) -> list[dict[str, Any]]:
        """Raw ``{provider, source}`` entries (source = plugin name)."""
        return list(self._ui_providers)

    @property
    def contribution_entries(self) -> list[dict[str, Any]]:
        """Declarative UI contributions as ``{kind, spec, source}`` entries.

        Collected from every activated plugin (Python API or manifest
        declaration) and pushed to the renderer inside ``plugin_ui_sync``.
        """
        return list(self._contributions)

    @property
    def command_entries(self) -> list[dict[str, Any]]:
        """Plugin-registered slash-command definitions (``plugin`` = source).

        Merged into the ``slash_commands`` config frame by the protocol
        handler so the renderer's existing ``applyServerCommands`` path
        consumes them with no extra plumbing.
        """
        return list(self._commands.values())

    @property
    def tool_meta_entries(self) -> list[dict[str, Any]]:
        """Merged tool UI metadata entries (``{name, meta, source}``).

        Populated from :func:`encre.plugins.tool_meta.collect_tool_meta`
        once per activation round, so :mod:`encre.transport.ws` can push
        both UI providers and tool metadata in a single WS event.
        """
        return list(self._tool_meta_entries)

    def set_tool_meta_entries(self, entries: Any) -> None:
        """Replace the tool UI metadata entries."""
        self._tool_meta_entries = [e for e in (entries or []) if isinstance(e, dict)]

    @property
    def gateway_adapters(self) -> list[Any]:
        return list(self._gateway_adapters)

    @property
    def memory_stores(self) -> list[Any]:
        return list(self._memory_stores)

    @property
    def ws_handlers(self) -> dict[str, Callable[..., Any]]:
        return dict(self._ws_handlers)

    def ws_handlers_for(self, plugin: str) -> dict[str, Callable[..., Any]]:
        """Snapshot of one plugin's WS handlers (empty for unknown plugins)."""
        return dict(self._ws_handlers_by_plugin.get(plugin, {}))

    def get_ws_handler(self, message_type: str, plugin: str = "") -> Callable[..., Any] | None:
        """Return the handler for ``(plugin, message_type)``.

        With *plugin* supplied the lookup is strictly namespaced: a plugin
        cannot resolve (and therefore cannot receive) another plugin's
        handler.  Without it, the legacy flat map is used.
        """
        if plugin:
            return self._ws_handlers_by_plugin.get(plugin, {}).get(message_type)
        return self._ws_handlers.get(message_type)

    @property
    def http_routes(self) -> list[dict[str, Any]]:
        return list(self._http_routes)

    @property
    def lifecycle_hooks(self) -> dict[str, list[Callable[..., Any]]]:
        return dict(self._lifecycle_hooks)

    # ── helpers ─────────────────────────────────────────────────

    @staticmethod
    def _remove_item(lst: list[Any], item: Any) -> None:
        with contextlib.suppress(Exception):
            lst.remove(item)
