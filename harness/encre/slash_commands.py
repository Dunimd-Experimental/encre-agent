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

"""Encre slash-command registry.

Slash commands (``/plan``, ``/spec``, project/user ``*.md`` command files, ...)
are modelled as :class:`SlashCommandDef` records and aggregated in
:class:`EncreCommandRegistry`, which merges sources by priority
(managed < user < project < bundled).  Every command has exactly one name;
aliases are not supported.  This module also parses terminal-only commands
and exposes helpers used by the desktop UI to render the command palette.

The registry supports two kinds of commands:

- **Modes** — change the agent's execution mode (e.g. ``plan``, ``spec``).
  Only the two built-in commands are modes; file-based commands are always
  actions, even if their frontmatter says ``kind: mode``, to prevent user
  error from accidentally enabling mode-switching behaviour.
- **Actions** — execute a single prompt or perform a discrete operation
  (e.g. ``init``, ``new``, ``clear``).

Commands are loaded from multiple sources:

1. **Builtin** — hard-coded entries in :data:`BUILTIN_SLASH_COMMANDS`.
2. **Project** — ``*.md`` files in ``.encre/commands/`` or ``.claude/commands/``
   under the active workspace.
3. **User** — ``*.md`` files in ``~/.encre/commands/`` or ``~/.claude/commands/``,
   plus ``custom_slash_commands`` from ``settings.json``.

The terminal layer (CLI) has its own separate set of commands
(:data:`TERMINAL_SLASH_COMMANDS`) that are not part of the main registry.
"""

import os
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from encre.prompts.loader import PromptLoader
from encre.skills.registry import parse_yaml_frontmatter

# Provider hook: the core layer (server) injects the loader for user-defined
# slash commands at startup. The harness must not import core modules, so this
# indirection avoids a circular dependency between the two packages.
_CUSTOM_COMMANDS_PROVIDER = None


def set_custom_commands_provider(provider) -> None:
    """Register the loader used to fetch user-defined slash commands.

    Called once during application startup by the core layer to inject a
    callable that returns a list of custom command dicts.  The harness
    cannot import core modules directly, so this dependency-injection
    pattern breaks the circular import chain.

    Args:
        provider: A callable that returns a list of dict entries, each
            containing keys like ``name``, ``title``, ``description``,
            ``icon``, ``argument_hint``, and ``prompt``.
    """
    global _CUSTOM_COMMANDS_PROVIDER
    _CUSTOM_COMMANDS_PROVIDER = provider


def _get_custom_commands() -> list:
    """Fetch user-defined commands via the injected provider (empty if unset).

    Returns an empty list when no provider has been registered, which
    causes the user-command loading path to gracefully skip this source
    without raising an error.
    """
    if _CUSTOM_COMMANDS_PROVIDER is None:
        return []
    return _CUSTOM_COMMANDS_PROVIDER()


class CommandSource(str, Enum):
    """Origin of a slash-command definition (lower number = higher precedence).

    The numeric ordering is determined by :data:`_COMMAND_PRIORITY` below
    and governs conflict resolution during registration.  A command loaded
    from a higher-precedence source (e.g. ``MANAGED``) silently replaces a
    same-named command from a lower-precedence source (e.g. ``BUNDLED``).
    """

    MANAGED = "managed"
    USER = "user"
    PROJECT = "project"
    BUNDLED = "bundled"


# Priority ordering for command sources: managed overrides user, user overrides
# project, project overrides bundled.  Used during registration conflict resolution.
_COMMAND_PRIORITY: dict[CommandSource, int] = {
    CommandSource.MANAGED: 0,
    CommandSource.USER: 1,
    CommandSource.PROJECT: 2,
    CommandSource.BUNDLED: 3,
}


@dataclass(frozen=True)
class SlashCommandDef:
    """Immutable definition of a single slash command.

    A frozen dataclass so that command definitions can be safely shared
    across threads and stored in registries without risk of accidental
    mutation.  The ``to_dict()`` method provides a serialisable view for
    the frontend command palette.

    File-backed commands carry a ``prompt_body`` (the markdown content after
    the frontmatter) and a ``file_path``; builtin commands typically have
    an empty ``prompt_body`` and ``file_path``.  The ``availability`` tuple
    restricts which modes a command is visible in (empty tuple = visible
    everywhere).

    Attributes:
        name: The command name (e.g. ``"plan"``, ``"init"``).
        title: Display name for the UI (e.g. ``"Plan Mode"``).
        description: Short summary of what the command does.
        kind: ``"mode"`` for mode-switching commands or ``"action"`` for
            single-operation commands.
        icon: Icon identifier used by the UI (defaults to ``"wand-2"``).
        argument_hint: Usage hint for the argument string.
        prompt_body: The rendered prompt body (for file-backed commands).
        source: Provenance of this command definition.
        file_path: Path to the source markdown file (empty for builtins).
        availability: Tuple of mode names this command is visible in.
    """
    name: str
    title: str
    description: str
    kind: Literal["mode", "action"]
    # Default glyph for user-defined commands mirrors the sidebar "Skills"
    # panel.  ``command`` (macOS ⌘) is intentionally never used.
    icon: str = "wand-2"
    # Extended fields -- populated for file-backed commands, empty for
    # the hard-coded BUILTIN set.  Default values keep the dataclass
    # backwards-compatible with all existing call sites.
    argument_hint: str = ""
    prompt_body: str = ""
    source: CommandSource = CommandSource.BUNDLED
    file_path: str = ""
    availability: tuple[str, ...] = ()  # empty = all modes; e.g. ("iwork",)

    def to_dict(self) -> dict:
        """Serialise this command definition into a plain dict for the frontend.

        Returns a JSON-serialisable representation suitable for the desktop
        UI command palette.  The ``has_prompt_body`` boolean lets the frontend
        distinguish between commands that have a prompt to render and those
        that are purely mode switches or actions.

        Returns:
            A dict with keys: ``name``, ``title``, ``description``, ``kind``,
            ``icon``, ``argument_hint``, ``source``, ``has_prompt_body``,
            ``prompt``, ``file_path``, and ``availability``.
        """
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "kind": self.kind,
            "icon": self.icon,
            "argument_hint": self.argument_hint,
            "source": self.source.value,
            "has_prompt_body": bool(self.prompt_body),
            "prompt": self.prompt_body,
            "file_path": self.file_path,
            "availability": list(self.availability),
        }

    def render_prompt(self, args: str = "") -> str:
        """Return the prompt body with ``{{args}}`` substituted.

        Mirrors the ``{{args}}`` / ``{{arguments}}`` / ``{{user_input}}``
        placeholders supported by :mod:`encre.skills.registry` so command
        authors can use the same conventions for both surfaces.  When the
        command has no prompt body (builtin mode commands), the empty string
        is returned unchanged.

        Args:
            args: The raw user argument string to substitute into placeholders.

        Returns:
            The rendered prompt body with all argument placeholders replaced.
        """
        body = self.prompt_body
        if not body:
            return body
        token = args or ""
        for placeholder in ("{{args}}", "{{arguments}}", "{{user_input}}"):
            body = body.replace(placeholder, token)
        return body


_prompt_loader = PromptLoader()

BUILTIN_SLASH_COMMANDS: tuple[SlashCommandDef, ...] = (
    SlashCommandDef(
        "plan", "Plan Mode",
        "Plan first, then run once you are ready",
        "mode", "list-checks",
    ),
    SlashCommandDef(
        "spec", "Spec Mode",
        "Write a spec before implementation",
        "mode", "list-checks",
    ),
    SlashCommandDef(
        "new", "New Session",
        "Start a fresh conversation",
        "action", "plus-circle",
    ),
    SlashCommandDef(
        "init", "Init Project",
        "Analyze project and generate workspace configuration",
        "action", "wand-sparkles",
        availability=("iwork",),
        prompt_body=_prompt_loader.load("init", category=""),
    ),
)


TERMINAL_SLASH_COMMANDS: tuple[SlashCommandDef, ...] = (
    SlashCommandDef("exit", "Exit", "Exit the terminal", "action"),
    SlashCommandDef("quit", "Quit", "Quit the terminal", "action"),
    SlashCommandDef("new", "New Session", "Start a fresh session", "action"),
    SlashCommandDef("clear", "Clear Session", "Clear the current session", "action"),
)


# Project-level command directories scanned under the active workspace.
# Kept in lock-step with the equivalent skill candidates in
# :mod:`encre.agent` so users can drop commands into either layout.
_PROJECT_COMMAND_CANDIDATE_DIRS: tuple[str, ...] = (
    ".encre/commands",
    ".claude/commands",
)

# User-level (global) command directories, relative to the home directory.
# These are scanned once per agent (not reloaded on workspace switch) so a
# user's personal commands (e.g. ``~/.claude/commands/commit.md``) are always
# available regardless of the active workspace.
_USER_COMMAND_CANDIDATE_DIRS: tuple[str, ...] = (
    ".claude/commands",
    ".encre/commands",
)


class EncreCommandRegistry:
    """File-backed slash-command registry with priority-based merging.

    Maintains an internal dict mapping command names to
    :class:`SlashCommandDef` records.  Builtin entries
    (:attr:`CommandSource.BUNDLED`) are seeded automatically in
    :meth:`__init__` so they remain available even when no project or
    user commands are present.

    When two commands share the same name, the one with higher source
    priority (managed > user > project > bundled) wins.  A new command
    only replaces an existing one if it has strictly higher priority;
    ties are resolved in favour of the first-registered definition.

    The registry supports incremental updates via :meth:`clear_source`,
    which removes all commands from a specific source without disturbing
    entries from other sources.  This enables workspace switching to
    replace project commands without wiping user-level commands.
    """

    def __init__(self) -> None:
        """Initialise the registry and seed builtin commands."""
        self._commands: dict[str, SlashCommandDef] = {}
        for cmd in BUILTIN_SLASH_COMMANDS:
            self.register(cmd)

    def register(self, command: SlashCommandDef) -> None:
        """Register a command, replacing any same-named command of lower priority.

        A new command wins only if its source has strictly higher priority
        (lower number in :data:`_COMMAND_PRIORITY`).  Ties are resolved
        in favour of the first-registered definition, preventing later
        bundles from accidentally clobbering user-managed overrides.
        """
        existing = self._commands.get(command.name)
        if existing is not None:
            new_priority = _COMMAND_PRIORITY.get(command.source, 3)
            old_priority = _COMMAND_PRIORITY.get(existing.source, 3)
            if new_priority >= old_priority:
                return
        self._commands[command.name] = command

    def lookup(self, name: str) -> SlashCommandDef | None:
        """Resolve a command by exact name.

        Returns:
            The :class:`SlashCommandDef` for the given name, or ``None``
            when no such command is registered.
        """
        return self._commands.get(name)

    def list_all(self) -> list[SlashCommandDef]:
        """Return all registered commands as a list.

        The order is insertion order (dict order in CPython 3.7+), which
        reflects registration priority: later sources override earlier
        ones, so the list is sorted by effective priority.
        """
        return list(self._commands.values())

    def clear_non_builtin(self) -> None:
        """Drop every command that is not part of the bundled set.

        Used before reloading project/user commands so stale entries do
        not linger after a directory disappears.
        """
        self._commands = {
            name: cmd
            for name, cmd in self._commands.items()
            if cmd.source == CommandSource.BUNDLED
        }

    def clear_source(self, source: CommandSource) -> None:
        """Drop every command originating from *source*.

        Unlike :meth:`clear_non_builtin` this preserves other non-builtin
        sources -- e.g. clearing ``PROJECT`` on a workspace switch must not
        wipe user-level (``USER``) commands loaded from the home directory
        and ``settings.json``.
        """
        self._commands = {
            name: cmd
            for name, cmd in self._commands.items()
            if cmd.source != source
        }

    def load_from_dir(self, commands_dir: str, source: CommandSource) -> int:
        """Load ``*.md`` command files from *commands_dir*.

        Walks the directory, reads each ``*.md`` file, parses its YAML
        frontmatter, and registers the resulting :class:`SlashCommandDef`
        with the given *source*.  Files without a valid frontmatter
        ``name:`` field are skipped silently, matching the tolerant
        behaviour of the skills registry.

        Args:
            commands_dir: Path to the directory containing command files.
            source: The :class:`CommandSource` tag applied to all loaded
                commands.

        Returns:
            The number of commands successfully registered.
        """
        if not os.path.isdir(commands_dir):
            return 0
        loaded = 0
        for entry in sorted(os.listdir(commands_dir)):
            full = os.path.join(commands_dir, entry)
            if not os.path.isfile(full):
                continue
            if not entry.lower().endswith(".md"):
                continue
            try:
                with open(full, encoding="utf-8") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                # I/O failures are non-fatal; skip the file silently.
                continue
            cmd = _command_from_markdown(content, source, full)
            if cmd is None:
                continue
            self.register(cmd)
            loaded += 1
        return loaded


def _command_from_markdown(
    content: str, source: CommandSource, file_path: str
) -> SlashCommandDef | None:
    """Build a :class:`SlashCommandDef` from a markdown file's frontmatter.

    Parses YAML frontmatter to extract the command name, title, description,
    icon, and argument hint.  The ``kind`` field is always set to
    ``"action"`` regardless of what the frontmatter says, because user-defined
    modes could accidentally enable mode-switching behaviour (tool interception,
    spec gates, etc.) that would be confusing or unsafe.

    The ``icon`` field is normalised: ``"command"`` is remapped to
    ``"wand-2"`` because the ``command`` glyph is reserved for the macOS
    Command key and should not be used as a command icon.

    Args:
        content: The full text of the markdown file.
        source: The :class:`CommandSource` to tag this command with.
        file_path: Absolute path to the source file (stored for debugging).

    Returns:
        A :class:`SlashCommandDef` or ``None`` if the frontmatter is missing
        or lacks a valid ``name`` field.
    """
    metadata, body = parse_yaml_frontmatter(content)
    raw_name = str(metadata.get("name", "")).strip()
    if not raw_name:
        return None
    name = raw_name.lower().replace("_", "-")
    title = str(metadata.get("title", name)).strip() or name
    description = str(metadata.get("description", "")).strip()
    kind_raw = str(metadata.get("kind", "action")).strip().lower()
    # There are no user-defined modes -- only the two built-ins (plan/spec)
    # are modes.  File-based commands are always actions (commands), even if
    # a frontmatter ``kind: mode`` is present, so they never get mistaken for
    # modes (no tool interception, no spec gate, no set_mode).
    kind: Literal["mode", "action"] = "action"
    _ = kind_raw  # kept for clarity; intentionally ignored
    icon = str(metadata.get("icon", "wand-2")).strip() or "wand-2"
    if icon == "command":
        icon = "wand-2"
    argument_hint = str(metadata.get("argument_hint", "")).strip()
    return SlashCommandDef(
        name=name,
        title=title,
        description=description,
        kind=kind,
        icon=icon,
        argument_hint=argument_hint,
        prompt_body=body.strip(),
        source=source,
        file_path=file_path,
    )


def load_project_commands(
    workspace_path: str, registry: EncreCommandRegistry | None = None
) -> EncreCommandRegistry:
    """Load project-level commands for *workspace_path*.

    When *registry* is provided it is reused (and its **project** entries
    cleared first -- user-level entries are preserved).  Otherwise a fresh
    registry is created.  Existing builtin and user entries are preserved in
    either case.
    """
    reg = registry if registry is not None else EncreCommandRegistry()
    reg.clear_source(CommandSource.PROJECT)
    if not workspace_path:
        return reg
    for rel in _PROJECT_COMMAND_CANDIDATE_DIRS:
        full = os.path.join(workspace_path, rel)
        if os.path.isdir(full):
            reg.load_from_dir(full, CommandSource.PROJECT)
    return reg


def _command_from_settings_dict(d: dict, source: CommandSource,
                                file_path: str) -> SlashCommandDef | None:
    """Build a :class:`SlashCommandDef` from a settings.json command dict.

    Converts a JSON-sourced command entry (from the frontend-managed
    ``custom_slash_commands`` list in ``settings.json``) into a
    :class:`SlashCommandDef`.  The ``prompt`` and ``prompt_body`` keys
    are both accepted for backwards compatibility.

    Like :func:`_command_from_markdown`, the ``kind`` is always set to
    ``"action"`` and the ``"command"`` icon is remapped to ``"wand-2"``.

    Args:
        d: The raw dict from settings.json.
        source: The :class:`CommandSource` to tag this command with.
        file_path: Path string (empty for settings-sourced commands).

    Returns:
        A :class:`SlashCommandDef` or ``None`` if the dict lacks a valid ``name``.
    """
    raw_name = str(d.get("name", "")).strip()
    if not raw_name:
        return None
    name = raw_name.lower().replace("_", "-")
    title = str(d.get("title", name)).strip() or name
    description = str(d.get("description", "")).strip()
    icon = str(d.get("icon", "wand-2")).strip() or "wand-2"
    if icon == "command":
        icon = "wand-2"
    argument_hint = str(d.get("argument_hint", "")).strip()
    prompt_body = str(d.get("prompt", "") or d.get("prompt_body", "")).strip()
    # No user-defined modes -- settings commands are always actions.
    return SlashCommandDef(
        name=name,
        title=title,
        description=description,
        kind="action",
        icon=icon,
        argument_hint=argument_hint,
        prompt_body=prompt_body,
        source=source,
        file_path=file_path,
    )


def load_user_commands(
    registry: EncreCommandRegistry | None = None,
) -> EncreCommandRegistry:
    """Load user-level (global) commands into *registry*.

    Two sources, both tagged :attr:`CommandSource.USER`:

    * Home-directory ``*.md`` command files (``~/.claude/commands``,
      ``~/.encre/commands``) -- previously a dead code path
      (``CommandSource.USER`` had no loader).
    * ``custom_slash_commands`` stored in ``settings.json`` (frontend-managed
      JSON commands).

    User commands override project and bundled commands of the same name
    (``USER`` has higher priority than ``PROJECT``/``BUNDLED``).  This is
    called once per agent and is NOT re-run on workspace switch, so a user's
    personal commands persist across workspaces.  Existing ``USER`` entries
    are cleared first so a reload stays fresh.
    """
    reg = registry if registry is not None else EncreCommandRegistry()
    reg.clear_source(CommandSource.USER)
    home = os.path.expanduser("~")
    for rel in _USER_COMMAND_CANDIDATE_DIRS:
        full = os.path.join(home, rel)
        if os.path.isdir(full):
            reg.load_from_dir(full, CommandSource.USER)
    # Ingest settings.json custom_slash_commands so the backend registry is
    # the single source of truth surfaced via get_slash_command_defs.  The
    # loader is injected by the core layer via set_custom_commands_provider.
    for entry in _get_custom_commands():
        if not isinstance(entry, dict):
            continue
        cmd = _command_from_settings_dict(entry, CommandSource.USER, "")
        if cmd is not None:
            reg.register(cmd)
    return reg


def get_slash_command_defs(
    registry: EncreCommandRegistry | None = None,
) -> list[dict]:
    """Return all slash commands as plain dicts for the frontend.

    When *registry* is supplied, its commands are serialised via
    :meth:`SlashCommandDef.to_dict` and returned.  This includes both
    builtin and non-builtin commands, with higher-priority sources having
    already overridden same-named lower-priority commands during
    registration.  When *registry* is ``None``, only the builtin commands
    are returned, which is useful for the initial UI load before any
    workspace-specific commands are loaded.

    Args:
        registry: Optional :class:`EncreCommandRegistry` to read from.

    Returns:
        List of command dicts suitable for JSON serialisation.
    """
    if registry is None:
        return [cmd.to_dict() for cmd in BUILTIN_SLASH_COMMANDS]
    return [cmd.to_dict() for cmd in registry.list_all()]


def parse_terminal_slash_command(prompt: str) -> SlashCommandDef | None:
    """Parse a terminal prompt and return the matching slash command, if any.

    Only the built-in terminal commands (:data:`TERMINAL_SLASH_COMMANDS`)
    are recognised.  These are parsed separately from the main registry
    because they are exclusive to the CLI/terminal interface and should
    not appear in the desktop UI's command palette.

    Args:
        prompt: The raw user input string, which may or may not start
            with a ``/`` character.

    Returns:
        The matching :class:`SlashCommandDef`, or ``None`` when the input
        does not start with ``/`` or does not match any terminal command.
    """
    text = prompt.strip()
    if not text.startswith("/"):
        return None
    name = text[1:].strip().lower()
    for cmd in TERMINAL_SLASH_COMMANDS:
        if cmd.name == name:
            return cmd
    return None
