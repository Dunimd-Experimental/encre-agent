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

"""Tool UI metadata — the authoritative, data-driven description of how each
tool is presented in the desktop chat UI.

This module replaces the frontend's hard-coded presentation tables (a 60+ branch
icon switch, a terminal-label table, a display-name table and a family of
"is this a terminal / file / read / item / hidden tool?" heuristics).  Each
entry is plain data so a plugin can override any of it without shipping a
renderer, and so the desktop stops learning about tool names.

Shape
-----
``ToolUi``
    The per-tool presentation record.  ``card`` selects a fixed card family,
    ``body`` selects one of the registered ``chat.tool.body`` renderers, and
    ``result`` selects how a summary line is derived.  Prefix entries
    (``prefix=True``) match tool names by prefix, which covers the whole family
    of task/cron/memory/mcp tools with one record.

``BUILTIN_TOOL_UI``
    The catalog of every tool the client knows about.  A builtin ``EncreTool``
    that declares its own ``ui=`` in ``build_tool`` wins over its catalog entry,
    which keeps each tool's presentation next to its implementation.

``ui_dict(...)``
    Convenience constructor so call sites can stay declarative and inline.
"""

from dataclasses import dataclass
from typing import Any

# Card families.  The renderer switches on exactly this set — never on tool
# names — which keeps the frontend free of any per-tool knowledge.
CARD_STRIP = "strip"
CARD_ITEM = "item"
CARD_FILE = "file"
CARD_AGENT = "agent"
CARD_QUESTION = "question"
CARD_INFO = "info"
CARD_HIDDEN = "hidden"
# Generic fault card for a plugin-provided tool that failed at runtime.  The
# card is not chosen by any tool name -- the frontend switches to it whenever a
# plugin-sourced tool call ends in error, so every plugin shares one error UI.
CARD_PLUGIN_ERROR = "plugin-error"

CARDS = frozenset(
    {
        CARD_STRIP,
        CARD_ITEM,
        CARD_FILE,
        CARD_AGENT,
        CARD_QUESTION,
        CARD_INFO,
        CARD_HIDDEN,
        CARD_PLUGIN_ERROR,
    }
)

# Body renderers.  These map 1:1 to the ``chat.tool.body`` frontend slot kinds.
BODY_PRE = "pre"
BODY_TERMINAL = "terminal"
BODY_DIFF = "diff"
BODY_WEB_RESULTS = "web-results"
BODY_WEB_FETCHED = "web-fetched"

BODIES = frozenset({BODY_PRE, BODY_TERMINAL, BODY_DIFF, BODY_WEB_RESULTS, BODY_WEB_FETCHED})

# Result-summary strategies for the one-line strip summary.
RESULT_TEXT = "text"
RESULT_JSON = "json"
RESULT_FILE = "file"
RESULT_MATCH_COUNT = "match_count"
RESULT_FILE_COUNT = "file_count"
RESULT_DISCOVER = "discover"
# Show ONLY the page URL a call targeted (params.url, or the labeled URL in
# the result head) — never the raw result, which is untrusted page content.
RESULT_URL = "url"


# Shell display names, keyed by the ``terminal`` argument of a terminal tool.
# The frontend used to keep this table in ``chat_tool_display.ts``.
TERMINAL_LABELS: dict[str, str] = {
    "auto": "Shell",
    "bash": "Bash",
    "cmd": "CMD",
    "powershell": "PowerShell",
    "pwsh": "pwsh",
    "python": "Python",
    "node": "Node.js",
    "irb": "Ruby",
    "julia": "Julia",
    "lua": "Lua",
    "php": "PHP",
    "R": "R",
}

_TERMINAL_KEYS = ["command", "cmd", "input", "shell_command", "script"]


@dataclass(frozen=True)
class ToolUi:
    """Presentation record for a single tool.

    ``prefix=True`` marks family entries (``task_``, ``cron_``, ``mcp__``) that
    match by name prefix instead of exactly.  ``terminal_labels`` is only used
    by terminal-shaped tools and resolves the display name from the ``terminal``
    argument the model passed.
    """

    icon: str | None = None
    label: str | None = None
    card: str = CARD_STRIP
    body: str = BODY_PRE
    hidden: bool = False
    prefix: bool = False
    result: str = RESULT_TEXT
    keys: tuple[str, ...] = ()
    terminal_labels: dict[str, str] | None = None
    hint: str | None = None

    def __post_init__(self) -> None:
        if self.card not in CARDS:
            raise ValueError(f"ToolUi.card {self.card!r} is not one of {sorted(CARDS)}")
        if self.body not in BODIES:
            raise ValueError(f"ToolUi.body {self.body!r} is not one of {sorted(BODIES)}")
        if self.hidden:
            object.__setattr__(self, "card", CARD_HIDDEN)

    def as_dict(self) -> dict[str, Any]:
        """Wire form.  ``None`` fields are dropped so the payload stays small."""
        out: dict[str, Any] = {
            "card": self.card,
            "body": self.body,
            "prefix": self.prefix,
            "result": self.result,
        }
        if self.icon:
            out["icon"] = self.icon
        if self.label:
            out["label"] = self.label
        if self.hidden:
            # Emitted explicitly so the frontend does not have to rely on the
            # ``card`` value ``__post_init__`` derives from this flag.
            out["hidden"] = True
        if self.keys:
            out["keys"] = list(self.keys)
        if self.terminal_labels:
            out["terminal_labels"] = dict(self.terminal_labels)
        if self.hint:
            out["hint"] = self.hint
        return out


def ui_dict(
    *,
    icon: str | None = None,
    label: str | None = None,
    card: str = CARD_STRIP,
    body: str = BODY_PRE,
    hidden: bool = False,
    prefix: bool = False,
    result: str = RESULT_TEXT,
    keys: tuple[str, ...] | list[str] | None = None,
    terminal_labels: dict[str, str] | None = None,
    hint: str | None = None,
) -> ToolUi:
    """Declarative constructor used by tool modules and plugin manifests."""
    return ToolUi(
        icon=icon,
        label=label,
        card=card,
        body=body,
        hidden=hidden,
        prefix=prefix,
        result=result,
        keys=tuple(keys or ()),
        terminal_labels=dict(terminal_labels) if terminal_labels else None,
        hint=hint,
    )


def _terminal(
    *,
    icon: str = "terminal",
    label: str | None = None,
    prefix: bool = False,
    result: str = RESULT_JSON,
    body: str = BODY_TERMINAL,
    terminal_labels: dict[str, str] | None = None,
) -> ToolUi:
    # ``terminal_labels`` is opt-in: only the shells that used to be renamed
    # from the ``terminal`` argument carry it, so a shell variant keeps its own
    # static label instead of inheriting the shell's name.
    return ui_dict(
        icon=icon,
        label=label,
        body=body,
        prefix=prefix,
        result=result,
        keys=tuple(_TERMINAL_KEYS),
        terminal_labels=terminal_labels,
    )


# --------------------------------------------------------------------------
# The catalog.  Every entry here used to be a branch of the frontend's
# getToolIcon / formatToolName / is*Tool heuristics.  Keep icons and labels
# identical to the previous frontend tables so the rendered UI is unchanged.
# --------------------------------------------------------------------------
BUILTIN_TOOL_UI: dict[str, ToolUi] = {
    # -- shell -----------------------------------------------------------
    "bash": _terminal(label="Bash", terminal_labels=TERMINAL_LABELS),
    "bash_output": _terminal(label="Bash Output"),
    "bash_kill": _terminal(label="Bash"),
    "bash_list": _terminal(label="Bash"),
    "shell": _terminal(label="Shell", terminal_labels=TERMINAL_LABELS),
    "terminal": _terminal(label="Terminal"),
    "run_command": _terminal(label="Bash", terminal_labels=TERMINAL_LABELS),
    "chat.terminal": _terminal(label="Bash", terminal_labels=TERMINAL_LABELS),
    "ssh": _terminal(body=BODY_PRE, result=RESULT_TEXT),
    # -- file mutation (card: strip, body: diff) --------------------------
    "write": ui_dict(icon="pencil-line", label="Write File", body=BODY_DIFF, result=RESULT_FILE),
    "file_write": ui_dict(icon="pencil-line", label="Write File", body=BODY_DIFF, result=RESULT_FILE),
    "write_file": ui_dict(icon="pencil-line", label="Write File", body=BODY_DIFF, result=RESULT_FILE),
    "edit": ui_dict(icon="pencil-line", label="Edit File", body=BODY_DIFF, result=RESULT_FILE),
    "file_edit": ui_dict(icon="pencil-line", label="Edit File", body=BODY_DIFF, result=RESULT_FILE),
    "edit_file": ui_dict(icon="pencil-line", label="Edit File", body=BODY_DIFF, result=RESULT_FILE),
    "apply_patch": ui_dict(
        # Patching a file is a mutation, but it is not one of the six core
        # mutation tools, so it keeps the compact item card and a plain body.
        icon="git-merge",
        label="Apply Patch",
        card=CARD_ITEM,
        body=BODY_PRE,
        result=RESULT_FILE,
    ),
    "delete_file": ui_dict(icon="trash-2", card=CARD_FILE, body=BODY_PRE, result=RESULT_FILE),
    # -- process management ----------------------------------------------
    "process_list": _terminal(label="Process List"),
    "process_kill": _terminal(label="Process Kill"),
    # -- file read -------------------------------------------------------
    "read": ui_dict(icon="eye", label="Read File", card=CARD_ITEM, result=RESULT_FILE),
    "file_read": ui_dict(icon="eye", label="Read File", card=CARD_ITEM, result=RESULT_FILE),
    "archive": ui_dict(icon="archive", card=CARD_STRIP),
    # -- web -------------------------------------------------------------
    "web_search": ui_dict(icon="globe", label="Web Search", body=BODY_WEB_RESULTS, keys=("query", "q")),
    "web_fetch": ui_dict(icon="cloud-download", label="Web Fetch", card=CARD_ITEM, body=BODY_WEB_FETCHED, keys=("url", "uri")),
    "rest_client": ui_dict(icon="send", label="Rest Client", card=CARD_ITEM),
    "cloud_storage": ui_dict(icon="hard-drive"),
    # -- search / discovery ---------------------------------------------
    "search": ui_dict(icon="search", result=RESULT_MATCH_COUNT, keys=("query", "pattern", "glob", "path")),
    "grep": ui_dict(icon="search", result=RESULT_MATCH_COUNT, keys=("query", "pattern", "glob", "path")),
    "glob": ui_dict(icon="file-search", result=RESULT_FILE_COUNT, keys=("query", "pattern", "glob", "path")),
    # -- memory ----------------------------------------------------------
    # Memory tools are hidden: they run silently in the background.
    "memory_profile": ui_dict(icon="user-circle", label="Memory Profile", hidden=True, keys=("query", "name", "path")),
    "memory": ui_dict(icon="database", label="Memory", hidden=True, keys=("query", "name", "path")),
    "memory_": ui_dict(
        icon="database",
        label="Memory",
        prefix=True,
        hidden=True,
        keys=("query", "name", "path"),
        hint="general.memoryAction",
    ),
    # -- task / cron / todo --------------------------------------------
    "task": ui_dict(
        icon="play-circle",
        label="Task",
        hidden=True,
        keys=("id", "title", "task_id"),
        hint="general.taskAction",
    ),
    "cron": ui_dict(
        icon="alarm-clock",
        label="Cron",
        hidden=True,
        keys=("schedule", "expression", "name"),
        hint="general.cronAction",
    ),
    "device": ui_dict(
        icon="monitor",
        label="Device",
        hidden=True,
        keys=("action",),
        hint="general.deviceAction",
    ),
    "todo": ui_dict(icon="list-todo", hidden=True),
    # -- delegation / skill / mcp --------------------------------------
    "skill": ui_dict(icon="wand-2", card=CARD_ITEM, keys=("name", "skill")),
    "mcp": ui_dict(
        icon="plug",
        label="MCP",
        card=CARD_ITEM,
        keys=("tool", "name", "function"),
        hint="general.selectToolCall",
    ),
    "mcp__": ui_dict(
        icon="plug",
        label="MCP",
        card=CARD_ITEM,
        prefix=True,
        keys=("tool", "name", "function"),
        hint="general.selectToolCall",
    ),
    "agent": ui_dict(icon="ea-tool-agent/assets/icon.svg", label="Agent", card=CARD_AGENT, keys=("prompt", "description", "agent_name")),
    "workflow": ui_dict(icon="workflow"),
    "plan": ui_dict(icon="file-text"),
    # -- code intel -----------------------------------------------------
    "lsp": ui_dict(icon="code", label="LSP", card=CARD_ITEM, hidden=True),
    "git": ui_dict(icon="git-branch", label="Git", card=CARD_ITEM),
    "github": ui_dict(icon="ea-tool-github/assets/icon.svg"),
    "diff": ui_dict(icon="file-diff"),
    "lint_format": ui_dict(icon="search-check"),
    "test_run": ui_dict(icon="flask-conical"),
    "json_tool": ui_dict(icon="ea-tool-json-tool/assets/icon.svg"),
    "env_manager": ui_dict(icon="key-round"),
    "notebook": ui_dict(icon="notebook-pen", card=CARD_ITEM),
    "manage": ui_dict(icon="layout-grid"),
    # -- data / docs / media --------------------------------------------
    "database": ui_dict(icon="database", card=CARD_ITEM),
    "spreadsheet": ui_dict(icon="table", card=CARD_ITEM),
    "pdf": ui_dict(icon="file-type-2", card=CARD_ITEM),
    "document": ui_dict(icon="file-text"),
    "presentation": ui_dict(icon="presentation"),
    "chart": ui_dict(icon="bar-chart-3"),
    "diagram": ui_dict(icon="git-fork"),
    # Only the canonical `image` tool gets the item card; the generate / edit /
    # variation aliases stay on the default strip card.
    "image": ui_dict(icon="image", card=CARD_ITEM),
    "generate_image": ui_dict(icon="image-plus"),
    "edit_image": ui_dict(icon="image"),
    "image_variation": ui_dict(icon="image"),
    "qr_code": ui_dict(icon="qr-code"),
    "media": ui_dict(icon="film"),
    "transcribe_audio": ui_dict(icon="mic"),
    "translate_audio": ui_dict(icon="mic"),
    "translation": ui_dict(icon="languages"),
    "hash_crypto": ui_dict(icon="hash"),
    # -- gui / infra ----------------------------------------------------
    "browser": ui_dict(icon="ea-tool-browser/assets/icon.svg", card=CARD_ITEM, result=RESULT_URL, keys=("url",)),
    "computer": ui_dict(icon="mouse-pointer-click", label="Computer", card=CARD_ITEM),
    "docker": ui_dict(icon="ea-tool-docker/assets/icon.svg", card=CARD_ITEM),
    "deploy": ui_dict(icon="cloud-upload", card=CARD_ITEM),
    "email": ui_dict(icon="mail"),
    "notify": ui_dict(icon="bell"),
    # -- api surfaces ---------------------------------------------------
    "file_api": ui_dict(icon="file"),
    "batch_api": ui_dict(icon="layers"),
    "fine_tuning_api": ui_dict(icon="sliders-horizontal"),
    "create_embeddings": ui_dict(icon="boxes"),
    "create_moderation": ui_dict(icon="shield-check"),
    # -- communication --------------------------------------------------
    "question": ui_dict(icon="circle-help", label="Question", card=CARD_QUESTION),
    "info": ui_dict(icon="badge-info", label="Info Card", card=CARD_INFO),
    "compact": ui_dict(icon="shrink", label="Compress"),
}


def get_builtin_ui(name: str) -> ToolUi | None:
    """Resolve a catalog entry: exact keys win, else the longest prefix family."""
    exact = BUILTIN_TOOL_UI.get(name)
    if exact is not None:
        return exact
    best_key = ""
    best_ui: ToolUi | None = None
    for key, ui in BUILTIN_TOOL_UI.items():
        if ui.prefix and name.startswith(key) and len(key) > len(best_key):
            best_key = key
            best_ui = ui
    return best_ui


def title_case_tool_name(name: str) -> str:
    """Fallback display name: ``fine_tuning_api`` -> ``Fine Tuning Api``."""
    return " ".join(w[:1].upper() + w[1:] for w in name.replace("_", " ").split(" "))


def resolve_label(ui: ToolUi, name: str, terminal: str | None = None) -> str:
    """Display name: terminal labels win, then the catalog label, then title-case."""
    if terminal and ui.terminal_labels:
        labeled = ui.terminal_labels.get(terminal)
        if labeled:
            return labeled
        return terminal[:1].upper() + terminal[1:]
    if ui.label:
        return ui.label
    if name.startswith("mcp__"):
        return "MCP"
    if name == "agent":
        return "Agent"
    return title_case_tool_name(name)


def all_ui_entries() -> list[dict[str, Any]]:
    """Every catalog entry as a wire record, keyed by tool name."""
    return [{"name": name, "meta": ui.as_dict()} for name, ui in BUILTIN_TOOL_UI.items()]
