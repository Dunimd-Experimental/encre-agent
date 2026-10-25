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

"""Platform adapter registry.

Lets platform adapters (built-in and plugin) self-register so the gateway can
discover and instantiate them without a hardcoded if/elif chain over platform
names.  :class:`encre.gateway.run.GatewayRunner` is the main consumer: it walks
:class:`encre.gateway.config.Platform`, asks the registry for an entry, and
calls :meth:`PlatformRegistry.create_adapter` to bring up every enabled
platform.

Built-in adapters register at module import time, at the bottom of their own
file.  Plugin adapters register through plugin discovery.  Heavy vendors (Discord,
Slack, Matrix SDKs) can instead register through
:meth:`PlatformRegistry.register_deferred`, which defers the SDK import until the
platform is actually looked up, keeping gateway startup fast.

Usage (adapter module)::

    from encre.gateway.platform_registry import platform_registry, PlatformEntry
    from encre.gateway.config import Platform

    platform_registry.register(PlatformEntry(
        name="telegram",
        label="Telegram",
        platform=Platform.TELEGRAM,
        adapter_factory=lambda cfg: TelegramAdapter(cfg),
        check_fn=check_telegram_requirements,
        required_env=["TELEGRAM_BOT_TOKEN"],
    ))

Usage (gateway runner)::

    adapter = platform_registry.create_adapter("telegram", platform_config)

Public API:
    * :class:`PlatformEntry` -- one platform's metadata and factory.
    * :class:`PlatformRegistry` -- the registry type.
    * :data:`platform_registry` -- the process-wide singleton.
"""

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from encre.gateway.config import Platform, PlatformConfig

logger = logging.getLogger("encre.gateway.platform_registry")


@dataclass
class PlatformEntry:
    """Metadata and factory for a single platform adapter.

    This is the whole contract a platform advertises.  Everything the runner
    needs to decide "can I start this platform, and how do I display it" is
    carried here, which is what allows the runner to stay platform-agnostic.

    Fields that look like strings but are env-var *names*
    (:attr:`allowed_users_env`, :attr:`allow_all_env`) exist so the runner and
    the setup UI can offer per-platform authorization controls without hardcoding
    a vendor-specific name table.  :mod:`encre.gateway.authz` uses its own
    derived naming scheme instead, so these two fields are advisory for the UI
    and the setup prompt.

    Attributes:
        name: Identifier used in config (e.g. "telegram", "discord").  Must
            match :attr:`encre.gateway.config.PlatformConfig` keys and the
            adapter's ``name`` attribute.
        label: Human-readable label (e.g. "Telegram", "Discord").  Used in logs
            and setup prompts; never used for routing.
        platform: The :class:`encre.gateway.config.Platform` enum value.
        adapter_factory: Callable that receives a :class:`PlatformConfig` and
            returns an adapter instance.
        check_fn: Returns True when the platform's dependencies are available
            (SDK installed, binaries present).  Called with no arguments and
            with no exception protection, so it must not raise.
        validate_config: Given a :class:`PlatformConfig`, is it properly
            configured?  ``None`` skips this stage.
        required_env: Env vars this platform needs (for setup display).
        install_hint: Hint shown when ``check_fn`` returns False.
        source: ``"builtin"`` or ``"plugin"``.  :meth:`PlatformRegistry.plugin_entries`
            filters on this.
        plugin_name: Name of the plugin that registered this entry.
        max_message_length: Platform's per-message size cap.  Used by the
            stream consumer and the delivery router before truncating.
        platform_hint: System prompt hint for this platform, injected when an
            agent session runs inside it.
        emoji: Display emoji for CLI/UI.
        allowed_users_env: Env var name for allowed user ids.
        allow_all_env: Env var name for allow-all flag.
    """

    name: str
    label: str
    platform: Platform | str
    adapter_factory: Callable[[PlatformConfig], Any]
    check_fn: Callable[[], bool]
    validate_config: Optional[Callable[[PlatformConfig], bool]] = None
    required_env: list[str] = field(default_factory=list)
    install_hint: str = ""
    source: str = "builtin"
    plugin_name: str = ""
    max_message_length: int = 0
    platform_hint: str = ""
    emoji: str = "🔌"
    allowed_users_env: str = ""
    allow_all_env: str = ""
    ui: dict = field(default_factory=dict)


@dataclass
class GatewayAdapterSpec:
    """Plugin-side description of one IM connector adapter (S8 contract).

    A plugin returns instances of this spec (or duck-typed equivalents, or a
    ready :class:`PlatformEntry`) from ``on_activate`` via
    ``context.gateway_adapters.register(spec)``.  The runner bridges each spec
    into the registry as a first-class :class:`PlatformEntry` with
    ``source="plugin"``, so plugin connectors ride the exact same
    start/stop/validate/status path as built-in adapters.

    Attributes:
        name: Registry name for the platform (e.g. ``"qqbot"``).  Doubles as
            the ``adapter_<name>_`` settings prefix and the platform identity
            when no :class:`Platform` enum member exists.
        label: Human-readable display label for logs and the adapter panel.
        adapter_factory: Callable receiving a :class:`PlatformConfig` and
            returning the adapter instance.
        check_fn: Dependency probe called before instantiation; must not raise.
        platform: Optional :class:`Platform` member or explicit string; when
            omitted the bridge falls back to ``name``.
        validate_config: Optional config validator; ``None`` skips validation.
        required_env: Env var names shown by the setup UI.
        install_hint: Hint displayed when ``check_fn`` returns False.
        max_message_length: Per-message size cap used by delivery truncation.
        platform_hint: System-prompt hint injected for sessions on this platform.
        emoji: Display emoji for CLI/UI surfaces.
        plugin_name: Name of the contributing plugin.  Filled by the plugin
            itself (the host accumulator does not retain the source argument),
            and used for ownership display and teardown filtering.
        ui: Declarative connector-card definition consumed by the desktop
            renderer (JSON-safe).  Shape: ``{"name": str, "desc": str,
            "docs": str, "qr": bool, "fields": [{"key": str, "labelKey": str,
            "type": "text"|"password"|"number"|"textarea",
            "placeholderKey": str}]}``.  ``labelKey``/``placeholderKey`` stay
            renderer-side locale keys so translations remain centralized;
            ``name``/``desc`` are fallbacks used when no locale entry exists.
    """

    name: str
    label: str
    adapter_factory: Callable[[PlatformConfig], Any]
    check_fn: Callable[[], bool]
    platform: Platform | str | None = None
    validate_config: Callable[[PlatformConfig], bool] | None = None
    required_env: list[str] = field(default_factory=list)
    install_hint: str = ""
    max_message_length: int = 0
    platform_hint: str = ""
    emoji: str = "🔌"
    plugin_name: str = ""
    ui: dict = field(default_factory=dict)


def register_plugin_adapter(spec: Any, plugin_name: str) -> str:
    """Bridge one plugin-provided connector spec into the platform registry.

    Accepts a :class:`GatewayAdapterSpec`, a duck-typed equivalent, or an
    already-built :class:`PlatformEntry`.  The bridged entry always carries
    ``source="plugin"`` and the contributing plugin's name so
    :meth:`PlatformRegistry.plugin_entries` can enumerate plugin connectors
    and the runner can stop them on plugin teardown.

    Args:
        spec: The connector description provided by the plugin.
        plugin_name: Name of the plugin contributing the spec (ownership).

    Returns:
        The registry name the adapter was registered under.

    Raises:
        ValueError: When the spec lacks a ``name`` or an ``adapter_factory``.
    """
    if isinstance(spec, PlatformEntry):
        entry = replace(spec, source="plugin", plugin_name=plugin_name)
        platform_registry.register(entry)
        return entry.name

    name = str(getattr(spec, "name", "") or "").strip()
    if not name:
        raise ValueError(f"plugin adapter spec from {plugin_name!r} has no name")
    factory = getattr(spec, "adapter_factory", None)
    if factory is None:
        raise ValueError(f"plugin adapter spec {name!r} has no adapter_factory")
    check_fn = getattr(spec, "check_fn", None)
    plugin_name = str(getattr(spec, "plugin_name", "") or "") or plugin_name
    platform = getattr(spec, "platform", None) or name
    entry = PlatformEntry(
        name=name,
        label=str(getattr(spec, "label", "") or name),
        platform=platform,
        adapter_factory=factory,
        check_fn=check_fn if check_fn is not None else (lambda: True),
        validate_config=getattr(spec, "validate_config", None),
        required_env=list(getattr(spec, "required_env", None) or []),
        install_hint=str(getattr(spec, "install_hint", "") or ""),
        source="plugin",
        plugin_name=plugin_name,
        max_message_length=int(getattr(spec, "max_message_length", 0) or 0),
        platform_hint=str(getattr(spec, "platform_hint", "") or ""),
        emoji=str(getattr(spec, "emoji", "") or "🔌"),
        ui=dict(getattr(spec, "ui", None) or {}),
    )
    platform_registry.register(entry)
    return entry.name


class PlatformRegistry:
    """Central registry of platform adapters.

    Two tables: :attr:`_entries` holds concrete registrations, and
    :attr:`_deferred` holds lazy loaders for platforms whose SDK has not been
    imported yet.  Lookups promote a deferred entry to a concrete one on first
    use.

    Thread-safety: reads are plain dict lookups, which are atomic under the GIL.
    Writes happen at startup during sequential discovery, so no lock is taken.
    Do not register concurrently with a lookup from another thread.

    Invariant: a name is never present in both tables at once.  Every write
    path removes the deferred loader before inserting a concrete entry.
    """

    def __init__(self) -> None:
        """Create both lookup tables empty, deferring all discovery to startup.

        No entries are read from a config file or discovered here because the
        registry is populated in two distinct phases -- concrete
        :meth:`register` calls for platforms whose SDK is importable, and
        :meth:`register_deferred` calls for the rest -- and mixing either into
        the constructor would force every consumer to import every vendor SDK.

        Invariant: on return neither table is populated, and the class
        contract that a name appears in at most one of them already holds
        vacuously. Both are fresh dicts rather than module-level singletons, so
        test fixtures can build an isolated registry without leaking registrations
        into the process-global one.

        Args:
            None.
        """
        self._entries: dict[str, PlatformEntry] = {}
        # Deferred loaders: name -> zero-arg callable that imports the adapter
        # module (which calls register() to populate _entries).
        self._deferred: dict[str, Callable[[], None]] = {}
        # Display labels for platforms whose adapter module has not been
        # imported yet.  A deferred loader carries no PlatformEntry, but the
        # adapter panel must still render a row (name + label) for every
        # known platform -- including ones whose heavy vendor SDK module has
        # never been imported.  Cleared when a concrete entry replaces the
        # loader (or the platform is unregistered).
        self._deferred_labels: dict[str, str] = {}

    # -- Deferred loading --

    def register_deferred(
        self, name: str, loader: Callable[[], None], label: str | None = None
    ) -> None:
        """Register a lazy loader for a platform not yet imported.

        The loader runs at most once, the first time the platform is looked up.
        A concrete registration takes precedence over a deferred one.

        Args:
            name: Platform name to defer.
            loader: Zero-argument callable that performs the import and calls
                :meth:`register`.  Its return value is ignored.
            label: Human-readable label shown by status listings while the
                platform is still deferred.  Falls back to the platform name
                when omitted; replaced by the real
                :attr:`PlatformEntry.label` once the loader runs.

        Note:
            If a concrete entry already exists, the loader is dropped on the
            floor.  That ordering is what makes a plugin able to shadow a heavy
            built-in: whichever is registered concretely first wins.
        """
        if name in self._entries:
            return
        self._deferred[name] = loader
        self._deferred_labels[name] = label or name

    def _resolve(self, name: str) -> None:
        """Run the deferred loader for a name if one is pending.

        The loader is popped before it is called, so a failing loader is not
        retried.  A transient import failure (a partially installed SDK, a
        missing native library) therefore removes the platform from the
        registry for the process lifetime; the warning logged here with
        ``exc_info`` is the only diagnosis available, and restarting the
        gateway is the only remedy.

        Args:
            name: Platform name to promote.
        """
        loader = self._deferred.pop(name, None)
        if loader is None:
            return
        try:
            loader()
        except Exception as e:
            logger.warning(
                "Deferred load of platform '%s' failed: %s", name, e, exc_info=True
            )

    def _resolve_all(self) -> None:
        """Run every pending deferred loader.

        Iterating over a copy is required because each :meth:`_resolve` mutates
        the deferred table by popping the entry it loaded.

        Args: none.

        Note:
            This imports every pending vendor SDK module.  Startup paths and
            per-connection status builds must use ``all_entries(resolve=False)``
            plus :meth:`pending_labels` instead, or the import cost of every
            heavy adapter lands on the caller.
        """
        if not self._deferred:
            return
        for name in list(self._deferred):
            self._resolve(name)

    # -- Registration --

    def register(self, entry: PlatformEntry) -> None:
        """Register a platform adapter entry.

        If an entry with the same name exists, it is replaced (last writer wins).

        Args:
            entry: The entry to install.  Its :attr:`PlatformEntry.name` is the
                registry key.

        Note:
            Any deferred loader for the same name is discarded, because a
            concrete entry satisfies the lookup the loader existed for.
            Re-registration is logged at INFO rather than an error, since it is
            an expected plugin shadowing the built-in.
        """
        self._deferred.pop(entry.name, None)
        self._deferred_labels.pop(entry.name, None)
        if entry.name in self._entries:
            prev = self._entries[entry.name]
            logger.info(
                "Platform '%s' re-registered (was %s, now %s)",
                entry.name, prev.source, entry.source,
            )
        self._entries[entry.name] = entry
        logger.debug("Registered platform adapter: %s (%s)", entry.name, entry.source)

    def unregister(self, name: str) -> bool:
        """Remove a platform entry.  Returns True if it existed.

        Also drops a pending deferred loader, so an unregister followed by a
        lookup does not resurrect the platform.

        Args:
            name: Platform name.

        Returns:
            Whether a concrete entry was removed.
        """
        self._deferred.pop(name, None)
        self._deferred_labels.pop(name, None)
        return self._entries.pop(name, None) is not None

    # -- Lookup --

    def get(self, name: str) -> Optional[PlatformEntry]:
        """Look up a platform entry by name.

        Args:
            name: Platform name.

        Returns:
            The entry, or ``None`` when unregistered.  A missing entry triggers
            a deferred load first, so this is the correct entry point for
            lazy platforms; do not read ``_entries`` directly from outside.
        """
        if name not in self._entries:
            self._resolve(name)
        return self._entries.get(name)

    def ensure_loaded(self, name: str) -> None:
        """Promote a deferred platform to a concrete entry, importing its module.

        A synchronous, explicit form of the promotion that :meth:`get` performs
        implicitly.  Exists so async callers can run the (potentially slow)
        vendor SDK import in a worker thread via ``asyncio.to_thread`` before
        touching the registry on the event loop.

        Args:
            name: Platform name to promote.  Unknown or already-concrete names
                are no-ops.
        """
        self._resolve(name)

    def all_entries(self, resolve: bool = True) -> list[PlatformEntry]:
        """Return all registered platform entries.

        Args:
            resolve: When True (default), promote every pending deferred
                loader first -- importing their adapter modules -- so the
                result is the complete platform surface.  When False, return
                only concrete entries; combine with :meth:`pending_labels` to
                still render a complete, import-free listing (the startup and
                per-connection status paths do exactly that).

        Returns:
            Every concrete entry, in registration order.  A fresh list is
            built on every call.
        """
        if resolve:
            self._resolve_all()
        return list(self._entries.values())

    def pending_labels(self) -> dict[str, str]:
        """Return display labels for platforms still deferred (not imported).

        Status listings merge this with a no-resolve :meth:`all_entries` so
        the adapter panel shows every known platform without paying the
        vendor SDK import cost for unconfigured ones.

        Returns:
            A fresh ``{name: label}`` mapping, in deferred registration order.
        """
        return dict(self._deferred_labels)

    def plugin_entries(self) -> list[PlatformEntry]:
        """Return only plugin-registered entries.

        Deferred loaders only ever exist for built-in platforms (they are
        created by :func:`encre.gateway.platforms.discover_platforms`), so a
        plugin entry is always concrete by the time it can be observed and
        no resolution is needed -- resolving here would import every pending
        vendor SDK for nothing.

        Args: none.

        Returns:
            Entries whose :attr:`PlatformEntry.source` is ``"plugin"``.
        """
        return [e for e in self._entries.values() if e.source == "plugin"]

    def is_registered(self, name: str) -> bool:
        """Check if a platform is registered (including deferred).

        Unlike :meth:`get`, this does not trigger a load, which makes it safe
        for a caller that only wants to know whether a platform is available
        without paying the SDK import cost.

        Args:
            name: Platform name.

        Returns:
            ``True`` if a concrete entry or a deferred loader exists.
        """
        return name in self._entries or name in self._deferred

    # -- Factory --

    def create_adapter(self, name: str, config: PlatformConfig) -> Optional[Any]:
        """Create an adapter instance for the given platform name.

        Runs the full admission chain in order: lookup -> dependency check ->
        config validation -> factory call.  Every stage logs its own reason and
        returns ``None`` so the runner can skip a platform and continue with
        the rest instead of aborting the whole gateway.

        Returns None if:
        - No entry registered for the name
        - check_fn() returns False (missing deps)
        - validate_config() returns False (misconfigured)
        - The factory raises an exception

        Args:
            name: Platform name, as used in config.
            config: The :class:`PlatformConfig` slice for this platform.

        Returns:
            A live adapter instance, or ``None``.

        Note:
            ``check_fn`` is called without exception protection, unlike
            ``validate_config`` and the factory.  A raising ``check_fn``
            propagates to the caller, which is deliberate: a dependency probe
            must be cheap and deterministic, so an exception means a broken
            entry rather than a transient platform problem.
        """
        if name not in self._entries:
            self._resolve(name)
        entry = self._entries.get(name)
        if entry is None:
            return None

        if not entry.check_fn():
            hint = f" ({entry.install_hint})" if entry.install_hint else ""
            logger.warning("Platform '%s' requirements not met%s", entry.label, hint)
            return None

        if entry.validate_config is not None:
            try:
                if not entry.validate_config(config):
                    logger.warning("Platform '%s' config validation failed", entry.label)
                    return None
            except Exception as e:
                logger.warning("Platform '%s' config validation error: %s", entry.label, e)
                return None

        try:
            adapter = entry.adapter_factory(config)
            return adapter
        except Exception as e:
            logger.error(
                "Failed to create adapter for platform '%s': %s",
                entry.label, e, exc_info=True,
            )
            return None


# Module-level singleton
platform_registry = PlatformRegistry()
