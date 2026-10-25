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

"""Platform adapter registry for the Encre IM gateway layer.

This package is the seam between Encre's agent core and every messaging
vendor it can talk to.  Each adapter subclasses
:class:`encre.gateway.platforms.base.BasePlatformAdapter`, normalises the
vendor's wire events into a :class:`encre.gateway.platforms.base.MessageEvent`,
and answers outbound requests through the adapter's ``send`` method whose
:class:`encre.gateway.platforms.base.SendResult` objects are consumed by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`.

The adapter for a given platform is a single module that performs its own
registration as an import side effect.  Importing ``telegram.adapter`` --
or any sibling -- has no visible return value; it mutates the platform
registry that :class:`encre.gateway.run.GatewayRunner` reads when it
decides which vendor backends to start for a given
``config.yaml``.  :func:`discover_platforms` is the single entry point
that triggers those side effects for every built-in adapter.

Data flow through this package:

1. ``discover_platforms()`` imports each adapter module (once per
   process).
2. :class:`encre.gateway.run.GatewayRunner` builds the adapters listed in
   configuration, injecting credentials and session routing.
3. An adapter's ``connect`` opens the vendor transport; inbound traffic is
   funnelled through :meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message`
   and dispatched to the registered handler.
4. Agent output returns through the adapter's ``send`` method.

Public API:
    * :func:`discover_platforms`
    * :class:`encre.gateway.platforms.base.BasePlatformAdapter`
    * :class:`encre.gateway.platforms.base.MessageEvent`
    * :class:`encre.gateway.platforms.base.MessageType`
    * :class:`encre.gateway.platforms.base.SendResult`
    * :func:`encre.gateway.platforms.base.classify_send_error`
    * :data:`encre.gateway.platforms.base.SEND_ERROR_KINDS`

The re-exports below exist so that gateway callers (``run``,
``stream_consumer``, ``session``) can import the shared data model from
this package root instead of reaching into
``encre.gateway.platforms.base`` directly.
"""

import importlib
import logging
from typing import List

from encre.gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    classify_send_error,
    SEND_ERROR_KINDS,
)

# Package-level logger; adapter modules log under the same
# ``encre.gateway.platforms`` hierarchy so a single handler covers discovery
# and every vendor.
logger = logging.getLogger("encre.gateway.platforms")

# All built-in platform module names (relative to encre.gateway.platforms).
# Order is not significant; each name maps to a module that registers its
# adapter on import.  Missing modules are skipped silently so that a single
# broken adapter never blocks gateway startup.
_BUILTIN_PLATFORMS = [
    # api_server resolves through _PLATFORM_MODULE_OVERRIDES below (its
    # implementation lives in the consolidated transport layer).  Every IM
    # connector is now shipped as an ea-connector-* plugin and registers
    # itself through the plugin host at warmup.
    "api_server",
]

# Explicit registry mapping platform name -> implementing module for
# adapters that live outside this package.  The OpenAI-compatible API
# server exposes the same inbound shape as an IM platform but is
# implemented by the consolidated transport layer
# (:mod:`encre.transport.http.openai_api`), so its platform entry is
# resolved there instead of by the default package-relative lookup.
_PLATFORM_MODULE_OVERRIDES = {
    "api_server": "encre.transport.http.openai_api",
}

# Display labels for deferred platforms.  Mirrors the ``label`` each adapter
# module passes to its own ``platform_registry.register`` call; used only
# while the module is not imported yet, so status listings can render the
# full adapter panel without importing any vendor SDK.  Once a platform's
# module is imported, the concrete entry's label always wins.
_PLATFORM_LABELS = {
    "api_server": "API Server",
    # All IM connectors (telegram, discord, slack, matrix, qqbot, dingtalk,
    # wecom, weixin, email, feishu, ... ) ship as ea-connector-* plugins;
    # each card and label now comes from the plugin's declarative ``ui``
    # spec via the gateway runner's status payload.
}

# Process-lifetime flag: discovery imports each adapter module exactly once,
# which is what keeps the import-time registration side effects idempotent
# when GatewayRunner and other entry points both ask for discovery.
_discovered = False


def _platform_module(name: str) -> str:
    """Resolve the importable module path for a platform name.

    Vendor adapters are looked up as
    ``encre.gateway.platforms.<name>``; names present in
    :data:`_PLATFORM_MODULE_OVERRIDES` are resolved to their out-of-package
    implementation instead.

    Args:
        name: Logical platform name, as it appears in
            :data:`_BUILTIN_PLATFORMS` and as the key of a platform entry in
            the gateway configuration.

    Returns:
        A fully-qualified module name suitable for
        :func:`importlib.import_module`.
    """
    return _PLATFORM_MODULE_OVERRIDES.get(name, f"encre.gateway.platforms.{name}")


def discover_platforms() -> List[str]:
    """Register every built-in platform as a deferred (lazy) registry entry.

    This is the single call site that activates the adapter registry at
    gateway startup.  Historically it imported every adapter module eagerly,
    which pulled in every vendor SDK at once -- the feishu adapter alone
    (``lark_oapi``) costs ~11s of CPU-bound import -- and stretched backend
    boot past 20s even when no gateway platform was configured.

    Now each platform name is handed to the registry as a deferred loader
    that imports its module on first real use: starting or configuring that
    one platform (:meth:`PlatformRegistry.get` / :meth:`~PlatformRegistry.ensure_loaded`).
    Import cost is paid per configured platform, in a worker thread, never
    for the 20+ platforms a user has not enabled.  Status listings render
    deferred platforms from their labels without importing anything.

    Args: none.

    Returns:
        The list of platform names known to the registry.  On repeat
        invocations the full :data:`_BUILTIN_PLATFORMS` list is returned, so
        callers can rely on the return value simply describing "all known
        platforms".

    Note:
        Import failures surface later and locally: a platform whose module
        raises on import logs a warning when (and only when) something
        actually tries to load it, instead of failing silently at startup
        while still paying the import attempt's cost for every process boot.
        Not thread-safe: the flag-and-register sequence is not guarded by a
        lock.  :class:`encre.gateway.run.GatewayRunner` calls this once
        during synchronous startup, before adapter tasks are spawned.
    """
    from encre.gateway.platform_registry import platform_registry

    global _discovered
    if _discovered:
        return _BUILTIN_PLATFORMS
    _discovered = True

    for name in _BUILTIN_PLATFORMS:
        # Bind the resolved module path now: the loop variable would
        # otherwise be captured by reference and every loader would import
        # the last platform's module.
        module = _platform_module(name)
        platform_registry.register_deferred(
            name,
            lambda m=module: importlib.import_module(m),
            label=_PLATFORM_LABELS.get(name, name),
        )
    logger.info(
        "Registered %d platform adapters (deferred import until first use)",
        len(_BUILTIN_PLATFORMS),
    )
    return list(_BUILTIN_PLATFORMS)


# Explicit package surface: the shared adapter contract and data model from
# encre.gateway.platforms.base, plus the discovery entry point.  Nothing
# else in this package is intended for external import, which keeps vendor
# adapters from accidentally coupling to discovery internals.
__all__ = [
    "BasePlatformAdapter",
    "MessageEvent",
    "MessageType",
    "SendResult",
    "classify_send_error",
    "SEND_ERROR_KINDS",
    "discover_platforms",
]
