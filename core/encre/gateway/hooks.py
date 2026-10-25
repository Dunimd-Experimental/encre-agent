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

"""Gateway hook registry: filesystem-discovered lifecycle event callbacks.

This is Encre's extension point inside the gateway layer.  A hook is a
directory under the user data dir containing a manifest and a handler module:

* ``HOOK.yaml`` -- manifest.  Read and validated, currently metadata only.
* ``handler.py`` -- module defining ``handle(event_type: str, context: dict)``.
  Optionally also an ``EVENTS`` list declaring which event types it wants.

Emitters live throughout the gateway: the runner fires ``gateway:startup``,
the session manager fires ``session:start`` / ``session:end`` /
``session:reset``, the agent loop bridge fires ``agent:start`` /
``agent:step`` / ``agent:end``, and the command dispatcher fires
``command:<canonical>`` or :data:`COMMAND_WILDCARD`.  The event type strings
in this module are the contract between those emitters and the hook authors,
so they must not be renamed casually.

Two dispatch modes exist:

* :meth:`HookRegistry.emit` -- fire and forget.  Return values are discarded.
  Used for observational hooks (logging, metrics, notifications).
* :meth:`HookRegistry.emit_collect` -- fire and gather non-``None`` return
  values.  Used for decision hooks, where a hook can answer
  ``{"decision": "allow" | "deny" | "handled" | "rewrite", ...}`` and the
  caller applies the first decisive answer.

Isolation guarantee: handlers are invoked one at a time inside their own
try/except, and a sync handler is awaited through the running event loop.  A
misbehaving hook logs a warning and is skipped; it can never abort a message.

Hook resolution: an exact event type match fires first, then a
``<base>:*`` wildcard.  A bare base such as ``"agent"`` does not match
``"agent:start"`` -- the wildcard suffix is required -- which keeps broad
subscription an explicit choice.
"""

import asyncio
import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from encre.config import get_data_dir

logger = logging.getLogger("encre.gateway.hooks")

# The directory user-installed hooks live in.  Computed at import time; the
# registry constructor accepts an override so tests can point at a temp dir.
HOOKS_DIR = Path(get_data_dir()) / "hooks"

# Named event-type constants.  These are matched as exact strings by
# HookRegistry._resolve_handlers, so their spelling is part of the hook API.
GATEWAY_STARTUP = "gateway:startup"
SESSION_START = "session:start"
SESSION_END = "session:end"
SESSION_RESET = "session:reset"
AGENT_START = "agent:start"
AGENT_STEP = "agent:step"
AGENT_END = "agent:end"
# command:<canonical> for a specific command; COMMAND_WILDCARD matches all.
COMMAND_WILDCARD = "command:*"


def _command_event(canonical: str) -> str:
    """Build the specific command event type for a canonical command name.

    Args:
        canonical: The command's canonical name, without a leading slash.

    Returns:
        The event type, e.g. ``"command:new"`` for ``/new``.  This resolves
        both to handlers registered for the specific command and, through
        wildcard matching, to handlers registered for :data:`COMMAND_WILDCARD`.
    """
    return f"command:{canonical}"


class HookRegistry:
    """Registry of lifecycle hooks, populated from disk or by registration.

    There is exactly one live instance per process, reachable through
    :func:`get_hook_registry`.  The registry is built once at gateway startup
    via :meth:`discover_and_load` and then only read while dispatching, which
    is why the handler lists are treated as effectively immutable after startup.

    State is a plain ``dict`` of event type to handler list.  Handlers are kept
    in registration order and invoked in that order, so a hook's relative
    priority is its discovery order (alphabetical by directory name, because
    :meth:`discover_and_load` iterates ``sorted()`` output).

    Thread safety: reads are plain dict reads and dispatch is serial within the
    event loop, so no locking is required.  Do not call :meth:`register`
    concurrently with :meth:`emit`.

    Args:
        hooks_dir: Directory to scan for hooks.  Defaults to :data:`HOOKS_DIR`.
    """

    def __init__(self, hooks_dir: str | Path | None = None) -> None:
        """Remember the hooks directory and start with empty handler tables.

        The directory is resolved through :func:`pathlib.Path` immediately so
        that later discovery code can concatenate onto it without worrying
        about string versus path mixing, and :data:`HOOKS_DIR` is only applied
        as a fallback when the caller passes ``None`` rather than an empty
        string, which would otherwise point discovery at the current directory.
        The handler map and the loaded-name list are both created empty because
        :meth:`discover_and_load` performs the import and registration step, and
        a registry constructed by a test must not pick up hooks from the
        developer's filesystem.

        Invariant: on return there is nothing dispatchable, so an :meth:`emit`
        before :meth:`discover_and_load` is a silent no-op instead of an error,
        which is what lets the gateway start platforms before hooks are scanned.

        Args:
            hooks_dir: Directory to scan for hook modules.  Defaults to
                :data:`encre.gateway.hooks.HOOKS_DIR`.
        """
        if hooks_dir is None:
            hooks_dir = HOOKS_DIR
        self._hooks_dir = Path(hooks_dir)
        # event_type -> list of callables.
        self._handlers: dict[str, list[Callable[..., Any]]] = {}
        self._loaded_hooks: list[str] = []

    @property
    def loaded_hooks(self) -> list[str]:
        """Names of the hooks successfully loaded from disk.

        Returns:
            A copy, so callers can iterate without affecting the registry's
            internal list.  A name appearing here means the module imported and
            exposed a callable ``handle``; it does not by itself mean the hook
            is subscribed to any event -- see :meth:`discover_and_load`.
        """
        return list(self._loaded_hooks)

    def register(self, event_type: str, handler: Callable[..., Any]) -> None:
        """Register ``handler`` for ``event_type`` programmatically.

        Appends to the event's handler list, so multiple handlers for one event
        type are all invoked, in order.  User hooks arrive here indirectly
        through :meth:`discover_and_load`; direct calls are for built-ins and
        tests.

        Args:
            event_type: Exact event type or a ``<base>:*`` wildcard.
            handler: Callable taking ``(event_type, context)``; sync or async.
        """
        self._handlers.setdefault(event_type, []).append(handler)

    def unregister(self, event_type: str, handler: Callable[..., Any]) -> None:
        """Remove a previously-registered handler (no-op if absent).

        Matches by identity (``is``), not equality, so a wrapper with an equal
        ``__eq__`` cannot accidentally detach another handler.  Rewrites the
        event's list rather than mutating it in place, because dispatch holds a
        reference to the list for the duration of one emit.

        Args:
            event_type: The event the handler was registered for.
            handler: The handler object to detach.
        """
        handlers = self._handlers.get(event_type)
        if handlers:
            self._handlers[event_type] = [h for h in handlers if h is not handler]

    def discover_and_load(self) -> list[str]:
        """Discover and load hooks from ``self._hooks_dir``.

        A hook is a subdirectory containing both a ``HOOK.yaml`` manifest and a
        ``handler.py`` module.  Directories starting with ``_`` or ``.`` are
        skipped, which is how a hook is disabled without deleting it.

        The handler module is inserted into ``sys.modules`` under the name
        ``encre_hooks.<name>`` *before* execution.  That matters for handlers
        whose source uses deferred typing (``from __future__ import
        annotations``, dataclasses, or third-party models that resolve forward
        references lazily): without an entry in ``sys.modules`` those
        resolutions raise.

        Subscription happens only through the module-level ``EVENTS`` list.  A
        module that omits ``EVENTS`` is still counted as loaded -- its import
        succeeded -- but receives no events, because nothing in this registry
        is importable from handler source.  Hook authors must declare ``EVENTS``.

        Any failure -- missing files, a manifest that is not a mapping, an import
        error, a missing ``handle`` -- is logged and skipped.  One broken hook
        never prevents the others from loading and never prevents the gateway
        from starting.

        Args: none.

        Returns:
            The names of hooks that loaded.  The handler table and the loaded
            list are cleared first, so this is the complete post-discovery
            picture, not an increment.
        """
        self._handlers.clear()
        self._loaded_hooks.clear()
        if not self._hooks_dir.exists():
            logger.info("[hooks] hooks dir %s does not exist -- no user hooks", self._hooks_dir)
            return []
        loaded: list[str] = []
        for entry in sorted(self._hooks_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("_") or entry.name.startswith("."):
                continue
            name = entry.name
            handler_path = entry / "handler.py"
            manifest_path = entry / "HOOK.yaml"
            if not handler_path.exists():
                logger.warning("[hooks] %s: missing handler.py -- skipping", name)
                continue
            if not manifest_path.exists():
                logger.warning("[hooks] %s: missing HOOK.yaml -- skipping", name)
                continue
            try:
                # Read manifest (currently metadata-only; validates it parses).
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = yaml.safe_load(f) or {}
                if not isinstance(manifest, dict):
                    raise ValueError("HOOK.yaml must be a mapping")
                # Load handler.py as a module.
                module_name = f"encre_hooks.{name}"
                spec = importlib.util.spec_from_file_location(module_name, handler_path)
                if spec is None or spec.loader is None:
                    raise RuntimeError("could not create module spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                handle_fn = getattr(module, "handle", None)
                if handle_fn is None or not callable(handle_fn):
                    logger.warning("[hooks] %s: handler.py has no callable handle() -- skipping", name)
                    continue
                # Subscription is declared by the module's EVENTS list; each
                # event type gets the same handle() callable registered for it.
                events = getattr(module, "EVENTS", None)
                if events:
                    for ev in events:
                        self.register(ev, handle_fn)
                loaded.append(name)
                logger.info("[hooks] loaded hook '%s' (manifest=%s)", name, manifest.get("name", name))
            except Exception as e:
                logger.warning("[hooks] failed to load hook '%s': %s %s", name, type(e).__name__, e)
        self._loaded_hooks = loaded
        return loaded

    def _resolve_handlers(self, event_type: str) -> list[Callable[..., Any]]:
        """Resolve handlers for an event: exact match first, then ``base:*``.

        Ordering is meaningful for decision hooks: an exact ``command:new``
        handler runs before the ``command:*`` handler, so a specific override
        can be answered by an earlier handler than a broad catch-all.

        Args:
            event_type: The event being dispatched.

        Returns:
            Handlers in invocation order, or an empty list.  A fresh list is
            built on every call, so registering or unregistering during a
            dispatch does not mutate the list being iterated.
        """
        handlers: list[Callable[..., Any]] = []
        exact = self._handlers.get(event_type)
        if exact:
            handlers.extend(exact)
        # Wildcard: "command:*" matches any "command:<x>"; a generic "<base>:*"
        # wildcard matches any "<base>:<x>".
        if ":" in event_type:
            base = event_type.split(":", 1)[0]
            wildcard = f"{base}:*"
            if wildcard != event_type:
                wild = self._handlers.get(wildcard)
                if wild:
                    handlers.extend(wild)
        return handlers

    async def emit(self, event_type: str, context: dict[str, Any] | None = None) -> None:
        """Fire ``event_type`` -- return values are discarded.

        Handlers run sequentially and each is awaited to completion before the
        next starts.  Sequential dispatch is deliberate: it gives deterministic
        ordering to observational hooks and keeps the event loop from stalling
        on a burst of concurrent hook I/O.  The tradeoff is that a slow hook
        delays the caller, so hooks must stay cheap; anything expensive should
        schedule work and return immediately.

        Args:
            event_type: The event name, e.g. :data:`SESSION_START`.
            context: Opaque mapping handed to every handler.  Handlers may read
                it but must not mutate it, since the same dict is passed to
                each handler in turn.
        """
        context = context or {}
        for handler in self._resolve_handlers(event_type):
            try:
                result = handler(event_type, context)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                logger.warning("[hooks] handler for %s raised: %s %s", event_type, type(e).__name__, e)

    async def emit_collect(self, event_type: str, context: dict[str, Any] | None = None) -> list[Any]:
        """Fire ``event_type`` and collect non-None return values.

        Used for decision-type hooks (e.g. ``command:*``): handlers return
        ``{"decision": "allow" | "deny" | "handled" | "rewrite", ...}`` and the
        caller decides based on the collected list (first decisive value wins).

        Returning ``None`` is the idiomatic way for a hook to abstain; it is
        dropped here so callers can iterate only over actual opinions.

        Args:
            event_type: The event name.
            context: Opaque mapping handed to every handler, same contract as
                :meth:`emit`.

        Returns:
            Non-``None`` handler results, in handler order.  Empty when no
            handler is registered or every handler abstained or raised.
        """
        context = context or {}
        results: list[Any] = []
        for handler in self._resolve_handlers(event_type):
            try:
                result = handler(event_type, context)
                if inspect.isawaitable(result):
                    result = await result
                if result is not None:
                    results.append(result)
            except Exception as e:
                logger.warning("[hooks] collector for %s raised: %s %s", event_type, type(e).__name__, e)
        return results


# Module-level singleton shared by the runner, the adapter base class, and the
# agent bridge.  Lazy creation keeps ``import encre.gateway.hooks`` free of
# filesystem side effects; the data dir may not exist at import time.
_registry: HookRegistry | None = None


def get_hook_registry() -> HookRegistry:
    """Return the shared :class:`HookRegistry` singleton.

    Lazily created on first call.  Callers (AdapterManager, EventRouter,
    BaseAdapter) use this to emit lifecycle events without holding their own
    reference.

    Returns:
        The process-wide registry.  Not thread-safe to create two of; the guard
        relies on the GIL making the check-and-assign sequence good enough for
        the single-startup call pattern.
    """
    global _registry
    if _registry is None:
        _registry = HookRegistry()
    return _registry


def reset_hook_registry(hooks_dir: str | Path | None = None) -> HookRegistry:
    """Reset the singleton (for tests / reconfiguration).

    Creates a fresh registry pointing at ``hooks_dir`` (defaults to the
    standard user hooks dir).  Does NOT discover -- call ``discover_and_load``
    afterwards.

    Args:
        hooks_dir: Optional replacement hooks directory.

    Returns:
        The newly installed registry.
    """
    global _registry
    _registry = HookRegistry(hooks_dir=hooks_dir)
    return _registry
