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

"""Gateway-side slash-command registry wrapper.

The authoritative slash-command definitions live in the harness under
:mod:`encre.slash_commands` (see :class:`SlashCommandDef`,
:class:`EncreCommandRegistry`, and the :data:`BUILTIN_SLASH_COMMANDS`
constant). This module is the *gateway-side* adapter that other parts of
:mod:`encre.gateway` import when they only need to answer three small
questions about a command name:

* Is this name registered as a known Encre slash command?
* What is the :class:`SlashCommandDef` for a given name, if any?
* Which commands should the Slack adapter expose to Slack as native
  slash commands (``/encre compress``, ``/encre clear``, …)?

The gateway does not execute slash commands itself; it decides whether an
inbound message should be routed to the harness for command execution
versus treated as plain prompt text. Keeping that decision here — rather
than duplicating it inside every channel adapter — means the desktop,
Slack, Telegram, and other surfaces all agree on which strings are
reserved.

The module exposes a single process-wide :class:`EncreCommandRegistry`
instance (``_registry``). The registry is populated from
:data:`BUILTIN_SLASH_COMMANDS` at import time by the harness; the helper
functions here are thin lookups over it.

Public API:

* :func:`register_command` — add a new command to the shared registry
  (used by tests and by late-loading platforms).
* :func:`is_gateway_known_command` — cheap "is this a slash command"
  check used by channel adapters to route inbound messages.
* :func:`get_command` — fetch a command definition by name.
* :func:`all_commands` — snapshot of the entire registry as a dict.
* :func:`slack_native_slashes` and :func:`slack_subcommand_map` — Slack
  adapter glue; Slack needs a flat ``/name → description`` list, and
  needs a mapping from bare subcommand names (``compress``, ``clear``)
  to their full ``/command`` strings.
"""

from __future__ import annotations

from typing import Callable

from encre.slash_commands import (
    BUILTIN_SLASH_COMMANDS,
    EncreCommandRegistry,
    SlashCommandDef,
)

# Process-wide command registry. This module is imported early by the
# gateway, so this object is created once at import time and shared for
# the lifetime of the process. The registry is a simple dict under the
# hood; concurrent writes from multiple threads are not guarded, but the
# command set is effectively frozen after startup — new registrations
# only happen during late-loading of optional platform adapters, which
# runs single-threaded during process boot.
_registry = EncreCommandRegistry()


def register_command(
    name: str,
    description: str,
    handler: Callable | None = None,
    subcommand_of: str | None = None,
) -> None:
    """Add a new slash-command definition to the shared gateway registry.

    This is the *gateway-side* registration hook. It is intentionally
    simple: it builds a :class:`SlashCommandDef` with ``kind="action"``
    and registers it in :data:`_registry`. The more elaborate definitions
    in :data:`BUILTIN_SLASH_COMMANDS` (which specify ``kind="info"``,
    ``kind="toggle"``, subcommand trees, and so on) are seeded by the
    harness before this module is imported, so this function is only used
    for commands that need to be added at runtime.

    The ``handler`` and ``subcommand_of`` arguments are accepted for API
    compatibility with the broader slash-command surface but are not used
    by the gateway registry itself — the gateway never invokes handlers,
    it only checks membership. Keep them in the signature so call sites
    that were ported from older code do not break.

    Args:
        name: Bare command name without the leading slash, e.g.
            ``"compress"``. The registry keys on this exact string.
        description: Short human-readable description shown in ``/help``
            output and in the desktop command palette.
        handler: Ignored by the gateway registry; retained so the
            signature matches the harness-side
            :meth:`EncreCommandRegistry.register` contract.
        subcommand_of: Ignored by the gateway registry; retained for
            the same reason.

    Returns:
        ``None``. Side effect: the new command is visible to subsequent
        :func:`is_gateway_known_command` and :func:`get_command` calls.
    """
    cmd = SlashCommandDef(
        name=name,
        title=name,
        description=description,
        kind="action",
    )
    _registry.register(cmd)


def is_gateway_known_command(name: str) -> bool:
    """Return ``True`` if *name* is a registered Encre slash command.

    Channel adapters use this to decide whether an inbound message should
    be interpreted as a slash command (and routed to the harness for
    execution) or as plain prompt text (and sent to the agent as a
    normal turn). It is called on every inbound message, so it must be
    cheap — the implementation is a single dict lookup.

    The broad exception clause is deliberate: a misconfigured registry
    or a partially-initialised registry should degrade to "unknown
    command" rather than raising into the channel adapter, because a
    channel crash is a much worse failure mode than silently dropping
    one message.

    Args:
        name: Bare command name, without the leading slash. Callers are
            responsible for stripping the ``/`` prefix before calling;
            this function does not do any normalisation.

    Returns:
        ``True`` if the registry contains a definition with this exact
        name; ``False`` otherwise, including when a lookup raises.
    """
    try:
        return _registry.lookup(name) is not None
    except Exception:
        return False


def get_command(name: str):
    """Return the :class:`SlashCommandDef` for *name*, or ``None``.

    Companion to :func:`is_gateway_known_command` for call sites that
    need the definition (description, title, kind, subcommand metadata)
    rather than just a boolean. Same exception-swallowing behaviour as
    :func:`is_gateway_known_command`: any registry error maps to
    ``None`` rather than propagating.

    Args:
        name: Bare command name.

    Returns:
        The :class:`SlashCommandDef` for this name, or ``None`` if the
        name is unknown or if the registry raised.
    """
    try:
        return _registry.lookup(name)
    except Exception:
        return None


def all_commands() -> dict[str, SlashCommandDef]:
    """Return a snapshot of the entire registry as a plain dict.

    The registry's internal store is a private ``_commands`` attribute;
    this wrapper hides that detail and hands callers a defensive copy so
    they can iterate the registry without holding a reference to the
    mutable internal structure. Mutation on the returned dict does not
    affect the underlying registry.

    Callers should treat the result as read-only — the registry may
    change underneath on a later tick, but this snapshot is what it was
    at call time.
    """
    return dict(_registry._commands)


def slack_native_slashes() -> list[tuple[str, str, Callable | None]]:
    """Return the list of slash commands to register with Slack.

    Slack requires each command to be registered with Slack's platform
    API before it can be invoked. This function walks
    :data:`BUILTIN_SLASH_COMMANDS` and reshapes each entry into Slack's
    ``(command_string, description, handler_or_None)`` tuple form. The
    handler is always ``None`` here because the Slack adapter receives
    the Slack HTTP callback and forwards it into the gateway, which
    dispatches to the harness — Slack itself does not hold a Python
    callable.

    The ``result or fallback`` at the end is a safety net: if the
    harness ships without any built-in slash commands (which would be a
    broken build), Slack would still get one command to register so the
    integration does not completely fail.

    Returns:
        A list of ``(command_string, description, None)`` tuples, in
        the order the harness seeded them. If the built-in list is
        empty, falls back to a single ``("/encre", "Encre Agent", None)``
        entry so Slack always has something to install.
    """
    from encre.skills.registry import get_shared_skill_registry

    result: list[tuple[str, str, Callable | None]] = []
    for cmd in BUILTIN_SLASH_COMMANDS:
        result.append((f"/{cmd.name}", cmd.description, None))
    return result or [("/encre", "Encre Agent", None)]


def slack_subcommand_map() -> dict[str, str]:
    """Return a mapping from bare subcommand names to their full slash strings.

    Slack commands take the form ``/encre compress`` — the ``/encre``
    part is the Slack command, and everything after it is a subcommand
    argument that the adapter has to translate back to the internal
    command name. This map is the lookup table that translates the
    Slack-parsed subcommand token (``"compress"``) to the internal
    command name (``"/compress"``). Aliases are folded here (``"compact"``
    and ``"compress"`` both map to ``"/compress"``, ``"reset"`` and
    ``"clear"`` both map to ``"/clear"``) so users can type either
    spelling.

    Only commands that make sense as Slack subcommands are listed;
    commands that are purely desktop-local (``/debug``, ``/internal``)
    are deliberately absent so they are not exposed to Slack users.
    """
    return {
        "compress": "/compress",
        "compact": "/compress",
        "clear": "/clear",
        "reset": "/clear",
        "status": "/status",
        "help": "/help",
    }
