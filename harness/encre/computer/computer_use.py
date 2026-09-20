#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

"""Unified high-level "computer-use" abstraction over browser and desktop.

Codex / Manus / Claude Code all expose a *single* curated action schema
to the model -- ``click``, ``type``, ``key``, ``scroll``, ``screenshot``,
``wait``, ``navigate`` -- and route it to either a browser or desktop
backend behind the scenes.  This module is Encre's equivalent:

    session = EncreComputerUseSession()
    result = await session.dispatch({
        "target": "browser",  # or "desktop"
        "action": "click_text",
        "text":   "Sign in",
    })

The session keeps a *trajectory* of past actions and their results
that the calling agent can feed back into the model's context, so a
multi-step computer-use loop can reason about what it has already tried
and avoid repeating itself.

The module deliberately stays thin: it does not capture screenshots or
build vision-language-model prompts.  That is the responsibility of the
agent / tool layer above; here we just provide the action dispatcher,
the trajectory, and a small validation layer.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("encre.computer.computer_use")

#: Default location of the persistent macro library on disk.
#: Can be overridden by setting the ``ENCRE_MACRO_LIBRARY`` env var
#: or by passing ``path=`` to :class:`MacroLibrary`.


def _default_macro_library_path() -> str:
    """Macro library path inside the data tree (was ``~/.encre/macros.json``)."""
    try:
        from encre.paths import get_data_dir

        return str(get_data_dir("macros.json", ensure=False))
    except Exception:  # pragma: no cover - defensive
        return str(Path.home() / ".dunimd" / "encre" / "macros.json")


DEFAULT_MACRO_LIBRARY_PATH = (
    os.environ.get("ENCRE_MACRO_LIBRARY") or _default_macro_library_path()
)

# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


#: Enumeration of well-known failure kinds.  ``UNCLASSIFIED`` is the
#: catch-all when we can't pin a more specific kind down from the
#: exception text.
class FailureKind:
    UNCLASSIFIED = "unclassified"
    ELEMENT_NOT_FOUND = "element_not_found"
    TIMEOUT = "timeout"
    NAVIGATION_ERROR = "navigation_error"
    JAVASCRIPT_ERROR = "javascript_error"
    NETWORK_ERROR = "network_error"
    PERMISSION_DENIED = "permission_denied"
    DIALOG_BLOCKED = "dialog_blocked"
    BROWSER_CLOSED = "browser_closed"
    DESKTOP_UNAVAILABLE = "desktop_unavailable"
    # Action returned success but the post-state hash matches the
    # pre-state hash -- the click / scroll / type hit a dead element
    # or the page already matched.  Worth retrying with a different
    # selector or via find_text fallback.
    NO_CHANGE = "no_change"


#: Substrings (lowercased) used to classify an exception's text into
#: a :class:`FailureKind`.  Order matters: the first match wins.
_FAILURE_PATTERNS: list[tuple[str, str]] = [
    ("element not found", FailureKind.ELEMENT_NOT_FOUND),
    ("no element matches", FailureKind.ELEMENT_NOT_FOUND),
    ("queryselector", FailureKind.ELEMENT_NOT_FOUND),
    ("strict mode violation", FailureKind.ELEMENT_NOT_FOUND),
    ("timeout", FailureKind.TIMEOUT),
    ("timed out", FailureKind.TIMEOUT),
    ("navigation failed", FailureKind.NAVIGATION_ERROR),
    ("net::err_", FailureKind.NETWORK_ERROR),
    ("econnrefused", FailureKind.NETWORK_ERROR),
    ("enotfound", FailureKind.NETWORK_ERROR),
    ("econnreset", FailureKind.NETWORK_ERROR),
    ("javascript error", FailureKind.JAVASCRIPT_ERROR),
    ("uncaught", FailureKind.JAVASCRIPT_ERROR),
    ("referenceerror", FailureKind.JAVASCRIPT_ERROR),
    ("typeerror", FailureKind.JAVASCRIPT_ERROR),
    ("permission denied", FailureKind.PERMISSION_DENIED),
    ("access denied", FailureKind.PERMISSION_DENIED),
    ("dialog", FailureKind.DIALOG_BLOCKED),
    ("browser has been closed", FailureKind.BROWSER_CLOSED),
    ("target closed", FailureKind.BROWSER_CLOSED),
    ("mss not installed", FailureKind.DESKTOP_UNAVAILABLE),
    ("pyautogui", FailureKind.DESKTOP_UNAVAILABLE),
]


def classify_failure(exc: BaseException) -> str:
    """Map an exception to a :class:`FailureKind` string.

    The classification is purely text-based and best-effort; it exists
    so the session can pick a smarter retry strategy (e.g. fall back
    to ``find_text`` only when the failure is an element lookup, not
    a network error).
    """
    msg = (str(exc) or type(exc).__name__ or "").lower()
    for needle, kind in _FAILURE_PATTERNS:
        if needle in msg:
            return kind
    return FailureKind.UNCLASSIFIED


# ---------------------------------------------------------------------------
# Persistent macro library
# ---------------------------------------------------------------------------


@dataclass
class MacroEntry:
    """A single named, versioned macro in the library.

    Fields
    ------
    name
        Unique identifier (case-sensitive).  Used as the dispatch key.
    actions
        Sequence of action dicts (same shape as the dispatch input).
    category
        Free-form grouping (e.g. ``"login"``, ``"search"``).  Used by
        :meth:`MacroLibrary.search`.
    version
        Integer that increments when the macro is updated.  Lets the
        agent detect when a stored macro has changed.
    description
        Human-readable summary of what the macro does.  Surfaced to
        the VLM so it can decide when to invoke a macro by name.
    author
        Free-form string identifying who/what wrote the macro.
    created_at
        Unix timestamp; set automatically on :meth:`register`.
    updated_at
        Unix timestamp; refreshed on every :meth:`update` call.
    tags
        List of free-form tags for search.
    """

    name: str
    actions: list[dict[str, Any]]
    category: str = "general"
    version: int = 1
    description: str = ""
    author: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise this macro entry to a JSON-friendly dictionary."""
        return {
            "name": self.name,
            "actions": list(self.actions),
            "category": self.category,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MacroEntry:
        """Build a :class:`MacroEntry` from a dict, validating required keys."""
        if not isinstance(payload, dict):
            raise TypeError("MacroEntry.from_dict: payload must be a dict")
        if not isinstance(payload.get("name"), str) or not payload["name"]:
            raise ValueError("MacroEntry.from_dict: 'name' is required")
        if not isinstance(payload.get("actions"), list):
            raise ValueError("MacroEntry.from_dict: 'actions' must be a list")
        return cls(
            name=payload["name"],
            actions=list(payload["actions"]),
            category=str(payload.get("category", "general")),
            version=int(payload.get("version", 1)),
            description=str(payload.get("description", "")),
            author=str(payload.get("author", "")),
            created_at=float(payload.get("created_at", time.time())),
            updated_at=float(payload.get("updated_at", time.time())),
            tags=[str(t) for t in (payload.get("tags") or [])],
        )


class MacroLibrary:
    """File-backed registry of named, versioned action sequences.

    Unlike the per-session :attr:`EncreComputerUseSession.macros`
    dict, a :class:`MacroLibrary` persists across sessions -- so a
    login macro registered on Monday is still there on Tuesday.
    Mirrors what Codex/Manus do with their shared "action recipes".
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | None = None) -> None:
        """Open the library at ``path`` (or the default), eager-loading it."""
        self._path = path or DEFAULT_MACRO_LIBRARY_PATH
        self._entries: dict[str, MacroEntry] = {}
        self._dirty = False
        # Best-effort eager load; failure is fine (means first save).
        with contextlib.suppress(Exception):
            self.load()

    # ----- path / persistence -----

    @property
    def path(self) -> str:
        """Return the on-disk path backing this macro library."""
        return self._path

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        """Write ``payload`` to disk encrypted (atomic temp file + rename)."""
        from encre.secure_io import write_json

        write_json(Path(self._path), payload)

    def save(self) -> int:
        """Persist the library to disk; returns number of entries saved."""
        payload = {
            "schema": self.SCHEMA_VERSION,
            "saved_at": time.time(),
            "entries": {
                name: entry.to_dict()
                for name, entry in self._entries.items()
            },
        }
        self._atomic_write(payload)
        self._dirty = False
        return len(self._entries)

    def load(self) -> int:
        """Load the library from disk; returns number of entries loaded."""
        target = Path(self._path)
        if not target.exists():
            return 0
        try:
            from encre.secure_io import read_json

            payload = read_json(target, default=None)
        except Exception as exc:
            logger.warning("MacroLibrary.load: read failed: %s", exc)
            return 0
        if payload is None:
            logger.warning(
                "MacroLibrary.load: %s is corrupt; ignoring", self._path,
            )
            return 0
        if not isinstance(payload, dict) or "entries" not in payload:
            logger.warning(
                "MacroLibrary.load: unexpected shape in %s; ignoring",
                self._path,
            )
            return 0
        loaded = 0
        for name, entry_payload in payload["entries"].items():
            try:
                entry = MacroEntry.from_dict(entry_payload)
                self._entries[entry.name] = entry
                loaded += 1
            except Exception as exc:
                logger.info(
                    "MacroLibrary.load: skipping %r: %s", name, exc,
                )
        self._dirty = False
        return loaded

    @property
    def is_dirty(self) -> bool:
        """True when there are unsaved changes."""
        return self._dirty

    # ----- CRUD -----

    def register(
        self,
        name: str,
        actions: list[dict[str, Any]],
        *,
        category: str = "general",
        description: str = "",
        author: str = "",
        tags: list[str] | None = None,
        overwrite: bool = False,
    ) -> MacroEntry:
        """Add or replace a macro.

        Raises :class:`KeyError` if the name already exists and
        ``overwrite`` is False; the existing version is preserved
        in that case (use :meth:`update` for a true upgrade path).
        """
        if not isinstance(name, str) or not name:
            raise ValueError("MacroLibrary.register: name required")
        if not isinstance(actions, list) or not all(
            isinstance(x, dict) for x in actions
        ):
            raise TypeError(
                "MacroLibrary.register: actions must be list[dict]"
            )
        if name in self._entries and not overwrite:
            raise KeyError(
                f"MacroLibrary.register: macro {name!r} already exists; "
                f"use update() or pass overwrite=True"
            )
        existing = self._entries.get(name)
        entry = MacroEntry(
            name=name,
            actions=list(actions),
            category=category,
            version=(
                (existing.version + 1) if existing is not None else 1
            ),
            description=description,
            author=author,
            created_at=(existing.created_at if existing is not None
                        else time.time()),
            updated_at=time.time(),
            tags=list(tags or []),
        )
        self._entries[name] = entry
        self._dirty = True
        return entry

    def update(
        self, name: str, actions: list[dict[str, Any]] | None = None,
        *, description: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> MacroEntry:
        """Update an existing macro (bumps version + updated_at)."""
        existing = self._entries.get(name)
        if existing is None:
            raise KeyError(
                f"MacroLibrary.update: macro {name!r} not found"
            )
        new_actions = (
            list(actions) if actions is not None
            else list(existing.actions)
        )
        new_desc = description if description is not None else existing.description
        new_cat = category if category is not None else existing.category
        new_tags = list(tags) if tags is not None else list(existing.tags)
        entry = MacroEntry(
            name=name,
            actions=new_actions,
            category=new_cat,
            version=existing.version + 1,
            description=new_desc,
            author=existing.author,
            created_at=existing.created_at,
            updated_at=time.time(),
            tags=new_tags,
        )
        self._entries[name] = entry
        self._dirty = True
        return entry

    def get(self, name: str) -> MacroEntry:
        """Return the macro named ``name`` or raise ``KeyError``."""
        if name not in self._entries:
            raise KeyError(f"MacroLibrary.get: macro {name!r} not found")
        return self._entries[name]

    def has(self, name: str) -> bool:
        """Return True if a macro named ``name`` exists."""
        return name in self._entries

    def remove(self, name: str) -> bool:
        """Remove the named macro; return True if it existed."""
        existed = self._entries.pop(name, None) is not None
        if existed:
            self._dirty = True
        return existed

    def list(self) -> list[MacroEntry]:
        """Return all macros sorted by (category, name)."""
        return sorted(
            self._entries.values(),
            key=lambda e: (e.category, e.name),
        )

    def names(self) -> list[str]:
        """Return all macro names, sorted alphabetically."""
        return sorted(self._entries.keys())

    def search(
        self,
        *,
        query: str | None = None,
        category: str | None = None,
        tag: str | None = None,
    ) -> list[MacroEntry]:
        """Filter macros by free-text query / category / tag.

        ``query`` does a case-insensitive substring match on name,
        description, and tags.
        """
        out: list[MacroEntry] = []
        q_lower = (query or "").lower()
        for entry in self._entries.values():
            if category and entry.category != category:
                continue
            if tag and tag not in entry.tags:
                continue
            if q_lower:
                hay = " ".join([
                    entry.name, entry.description, *entry.tags,
                ]).lower()
                if q_lower not in hay:
                    continue
            out.append(entry)
        return sorted(out, key=lambda e: (e.category, e.name))

    def import_from(
        self, payload: dict[str, Any] | str,
        *, overwrite: bool = False,
    ) -> int:
        """Bulk-import macros from a JSON object (or string)."""
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"MacroLibrary.import_from: not valid JSON: {exc}"
                ) from exc
        if not isinstance(payload, dict):
            raise TypeError(
                "MacroLibrary.import_from: payload must be a dict"
            )
        entries_payload = payload.get("entries", payload)
        if not isinstance(entries_payload, dict):
            raise TypeError(
                "MacroLibrary.import_from: 'entries' must be a dict"
            )
        added = 0
        for name, entry_payload in entries_payload.items():
            try:
                entry = MacroEntry.from_dict(entry_payload)
            except Exception as exc:
                logger.info(
                    "MacroLibrary.import_from: skipping %r: %s",
                    name, exc,
                )
                continue
            if entry.name in self._entries and not overwrite:
                continue
            self._entries[entry.name] = entry
            self._dirty = True
            added += 1
        return added

    def export(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of the library."""
        return {
            "schema": self.SCHEMA_VERSION,
            "exported_at": time.time(),
            "entries": {
                name: entry.to_dict()
                for name, entry in self._entries.items()
            },
        }

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: str) -> bool:
        return name in self._entries


#: Default retry / fallback policy per failure kind.  ``retries`` is
#: the *additional* attempts after the first failure; ``fallback`` is
#: the smart-recovery action to try (or ``None`` for "just retry").
DEFAULT_RETRY_POLICY: dict[str, dict[str, Any]] = {
    FailureKind.ELEMENT_NOT_FOUND: {
        "retries": 1,
        "fallback": "find_text",
    },
    FailureKind.TIMEOUT: {
        "retries": 2,
        "fallback": None,
    },
    FailureKind.NAVIGATION_ERROR: {
        "retries": 1,
        "fallback": None,
    },
    FailureKind.NETWORK_ERROR: {
        "retries": 3,
        "fallback": None,
    },
    FailureKind.JAVASCRIPT_ERROR: {
        "retries": 0,
        "fallback": None,
    },
    FailureKind.PERMISSION_DENIED: {
        "retries": 0,
        "fallback": None,
    },
    FailureKind.DIALOG_BLOCKED: {
        "retries": 0,
        "fallback": "set_dialog_handler",
    },
    FailureKind.BROWSER_CLOSED: {
        "retries": 0,
        "fallback": "reset_browser",
    },
    FailureKind.DESKTOP_UNAVAILABLE: {
        "retries": 0,
        "fallback": None,
    },
    FailureKind.NO_CHANGE: {
        "retries": 1,
        "fallback": "find_text",
    },
    FailureKind.UNCLASSIFIED: {
        "retries": 1,
        "fallback": None,
    },
}

# ---------------------------------------------------------------------------
# Action schema
# ---------------------------------------------------------------------------

#: Mapping of action -> default target backend.
#: The dispatcher falls back to ``session.default_target`` when the
#: caller didn't specify one and the action is ambiguous.
_ACTION_DEFAULT_TARGET: dict[str, str] = {
    # Browser-only
    "navigate": "browser",
    "go_back": "browser",
    "go_forward": "browser",
    "reload": "browser",
    "get_url": "browser",
    "get_title": "browser",
    "get_html": "browser",
    "get_text": "browser",
    "get_all_text": "browser",
    "get_attribute": "browser",
    "get_property": "browser",
    "execute_js": "browser",
    "select_option": "browser",
    "scroll_page": "browser",
    "list_tabs": "browser",
    "switch_tab": "browser",
    "new_tab": "browser",
    "close_tab": "browser",
    "set_dialog_handler": "browser",
    "set_file_chooser_handler": "browser",
    "a11y_snapshot": "browser",
    "click_by_role": "browser",
    "get_by_text_count": "browser",
    "get_page_structure": "browser",
    # Desktop-only
    "triple_click": "desktop",
    "clipboard_get": "desktop",
    "clipboard_set": "desktop",
    "file_drop": "desktop",
    "get_screen_size": "desktop",
    "get_cursor_position": "desktop",
    "accessibility_tree": "desktop",
    "find_element_by_name": "desktop",
    "get_elements": "desktop",
    "take_screenshot_png": "desktop",
    "locate_on_screen": "desktop",
    # Cross-target (default to session.default_target)
    "screenshot": "?",
    "click": "?",
    "double_click": "?",
    "right_click": "?",
    "click_at": "?",
    "double_click_at": "?",
    "right_click_at": "?",
    "hover": "?",
    "hover_at": "?",
    "type": "?",
    "type_at": "?",
    "press_key": "?",
    "key": "?",
    "hotkey": "?",
    "scroll": "?",
    "drag": "?",
    "move_mouse": "?",
    "click_text": "?",
    "find_text": "?",
    "wait": "?",
    "fill_form": "?",
    "done": "?",
    "screenshot_viewport": "browser",
}

#: All action names accepted by the dispatcher.
VALID_ACTIONS: frozenset[str] = frozenset(_ACTION_DEFAULT_TARGET.keys())


@dataclass
class ComputerUseAction:
    """A single normalised computer-use action.

    The dataclass holds the canonical fields most actions need; anything
    action-specific (form fields, dialog accept flag, etc.) goes into
    the ``extras`` dict.  Use :meth:`from_dict` to build one from a
    tool-call payload and :meth:`to_dict` to serialise it.
    """

    action: str
    target: str = "browser"
    # Coordinates
    x: int = 0
    y: int = 0
    x2: int = 0
    y2: int = 0
    # Text
    text: str = ""
    query: str = ""  # for click_text / find_text
    # Key / hotkey
    key: str = ""
    keys: list[str] = field(default_factory=list)
    # Scroll
    scroll_amount: int = 0
    # Form / selector
    selector: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    # Tab management
    tab_index: int = -1
    # Select
    option_value: str = ""
    option_by: str = "value"
    # DOM lookup
    attr_name: str = ""
    js_code: str = ""
    # Find-by-text
    fuzzy: bool = False
    exact: bool = False
    occurrence: int = 1
    # Wait
    ms: int = 0
    # Misc
    button: str = "left"
    coord_space: str = "auto"
    accept: bool = True
    prompt_text: str = ""
    paths: list[str] = field(default_factory=list)
    timeout: int | None = None
    # Anything else
    extras: dict[str, Any] = field(default_factory=dict)
    # Dry-run: when True, dispatch returns the *intended* effect
    # (target coordinates, selector resolution, etc.) without actually
    # mutating UI state.  This is the agent's "preview" mode -- it can
    # ask "if I click here, what would happen?" without committing.
    dry_run: bool = False
    # Expect-change: when True, dispatch verifies the DOM hash changed
    # after the action (browser target) or the screen pixels changed
    # (desktop target).  The result includes ``page_changed: bool``.
    # Useful for catching no-op clicks (e.g. click on a disabled
    # button that silently swallows the event).
    expect_change: bool = False
    # Per-action timeout in milliseconds.  Overrides the session
    # default.  ``None`` means "use session default".
    timeout_ms: int | None = None

    # ----- (de)serialisation -----

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ComputerUseAction:
        """Build an action from a flat dict (e.g. a tool-call payload)."""
        if not isinstance(payload, dict):
            raise TypeError("ComputerUseAction.from_dict expects a dict")
        action = str(payload.get("action") or "").strip().lower()
        if not action:
            raise ValueError("ComputerUseAction: 'action' is required")
        if action not in VALID_ACTIONS:
            raise ValueError(
                f"ComputerUseAction: unknown action {action!r}; "
                f"valid: {sorted(VALID_ACTIONS)}"
            )
        target = str(payload.get("target") or "").strip().lower()
        if target in ("", "auto", "default"):
            target = _ACTION_DEFAULT_TARGET.get(action, "?")
            if target == "?":
                target = "browser"
        if target not in ("browser", "desktop"):
            raise ValueError(
                f"ComputerUseAction: target must be 'browser' or 'desktop' "
                f"(got {target!r})"
            )
        # If the action is browser/desktop-locked and the caller asked
        # for the wrong target, snap it back to the canonical one -- a
        # clearer error than dispatching to the wrong backend.
        canonical = _ACTION_DEFAULT_TARGET.get(action, "?")
        if canonical in ("browser", "desktop") and canonical != target:
            logger.debug(
                "ComputerUseAction: snapping target %r -> %r for action %r",
                target, canonical, action,
            )
            target = canonical

        known_fields = {
            "action", "target", "x", "y", "x2", "y2", "text", "query",
            "key", "keys", "scroll_amount", "selector", "fields",
            "tab_index", "option_value", "option_by", "attr_name",
            "js_code", "fuzzy", "exact", "occurrence", "ms", "button",
            "coord_space", "accept", "prompt_text", "paths", "timeout",
            "dry_run", "expect_change", "timeout_ms",
        }
        extras = {k: v for k, v in payload.items() if k not in known_fields}
        return cls(
            action=action,
            target=target,
            x=int(payload.get("x") or 0),
            y=int(payload.get("y") or 0),
            x2=int(payload.get("x2") or 0),
            y2=int(payload.get("y2") or 0),
            text=str(payload.get("text") or ""),
            query=str(payload.get("query") or payload.get("name") or ""),
            key=str(payload.get("key") or ""),
            keys=[str(k) for k in (payload.get("keys") or [])],
            scroll_amount=int(payload.get("scroll_amount") or payload.get("clicks") or 0),
            selector=str(payload.get("selector") or ""),
            fields=dict(payload.get("fields") or {}),
            tab_index=int(payload.get("tab_index") or payload.get("index") or -1),
            option_value=str(payload.get("option_value") or payload.get("value") or ""),
            option_by=str(payload.get("option_by") or payload.get("by") or "value"),
            attr_name=str(payload.get("attr_name") or payload.get("name") or ""),
            js_code=str(payload.get("js_code") or payload.get("code") or ""),
            fuzzy=bool(payload.get("fuzzy", False)),
            exact=bool(payload.get("exact", False)),
            occurrence=int(payload.get("occurrence") or 1),
            ms=int(payload.get("ms") or 0),
            button=str(payload.get("button") or "left"),
            coord_space=str(payload.get("coord_space") or "auto"),
            accept=bool(payload.get("accept", True)),
            prompt_text=str(payload.get("prompt_text") or ""),
            paths=[str(p) for p in (payload.get("paths") or [])],
            timeout=(int(payload["timeout"])
                     if payload.get("timeout") is not None else None),
            dry_run=bool(payload.get("dry_run", False)),
            expect_change=bool(payload.get("expect_change", False)),
            timeout_ms=(int(payload["timeout_ms"])
                        if payload.get("timeout_ms") is not None else None),
            extras=extras,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a flat dict (round-trips with :meth:`from_dict`)."""
        out: dict[str, Any] = {
            "action": self.action,
            "target": self.target,
        }
        if self.x:
            out["x"] = self.x
        if self.y:
            out["y"] = self.y
        if self.x2:
            out["x2"] = self.x2
        if self.y2:
            out["y2"] = self.y2
        if self.text:
            out["text"] = self.text
        if self.query:
            out["query"] = self.query
        if self.key:
            out["key"] = self.key
        if self.keys:
            out["keys"] = list(self.keys)
        if self.scroll_amount:
            out["scroll_amount"] = self.scroll_amount
        if self.selector:
            out["selector"] = self.selector
        if self.fields:
            out["fields"] = dict(self.fields)
        if self.tab_index >= 0:
            out["tab_index"] = self.tab_index
        if self.option_value:
            out["option_value"] = self.option_value
        if self.option_by != "value":
            out["option_by"] = self.option_by
        if self.attr_name:
            out["attr_name"] = self.attr_name
        if self.js_code:
            out["js_code"] = self.js_code
        if self.fuzzy:
            out["fuzzy"] = True
        if self.exact:
            out["exact"] = True
        if self.occurrence != 1:
            out["occurrence"] = self.occurrence
        if self.ms:
            out["ms"] = self.ms
        if self.button != "left":
            out["button"] = self.button
        if self.coord_space != "auto":
            out["coord_space"] = self.coord_space
        if not self.accept:
            out["accept"] = False
        if self.prompt_text:
            out["prompt_text"] = self.prompt_text
        if self.paths:
            out["paths"] = list(self.paths)
        if self.timeout is not None:
            out["timeout"] = self.timeout
        if self.dry_run:
            out["dry_run"] = True
        if self.expect_change:
            out["expect_change"] = True
        if self.timeout_ms is not None:
            out["timeout_ms"] = self.timeout_ms
        out.update(self.extras)
        return out


# ---------------------------------------------------------------------------
# Trajectory
# ---------------------------------------------------------------------------


@dataclass
class ComputerUseStep:
    """One entry in the trajectory.

    ``screenshot_b64`` is optional -- the dispatcher only captures it
    for actions that take a fresh screenshot (``screenshot``,
    ``screenshot_viewport``, ``take_screenshot_png``) and when
    ``auto_screenshot`` is enabled on the session.
    """

    action: ComputerUseAction
    success: bool
    result: Any = None
    error: str = ""
    elapsed_ms: int = 0
    timestamp: float = field(default_factory=time.time)
    screenshot_b64: str = ""
    # Failure classification (set on failure): one of FailureKind.*.
    failure_kind: str = ""
    # DOM / screen-state diff (set when ``expect_change`` is on):
    # True if the world looks different from before the action.
    page_changed: bool | None = None
    # DOM / screen-state hash *before* the action ran.  Useful for
    # ``trajectory.page_diff_summary()``.
    pre_hash: str = ""
    # DOM / screen-state hash *after* the action ran.
    post_hash: str = ""
    # Number of additional retry attempts consumed before success.
    retries_used: int = 0
    # True when a smart fallback (e.g. find_text after failed click)
    # is what actually executed the action.
    fallback_used: bool = False

    def summary(self) -> str:
        """One-line human summary used in prompts / logs."""
        prefix = "OK" if self.success else "FAIL"
        target = self.action.target
        action = self.action.action
        if action == "click" and (self.action.x or self.action.y):
            tail = f"({self.action.x},{self.action.y})"
        elif action == "type":
            tail = repr(self.action.text[:40])
        elif action == "click_text":
            tail = repr(self.action.query[:40])
        elif action in ("navigate", "go_back", "go_forward", "reload"):
            tail = self.action.text or self.action.extras.get("url", "")
        else:
            tail = ""
        msg = f"[{prefix}] {target}.{action} {tail}".rstrip()
        if not self.success and self.failure_kind:
            msg += f" [{self.failure_kind}]"
        if self.page_changed is False:
            msg += " [no-op: page unchanged]"
        if self.fallback_used:
            msg += " [via-fallback]"
        if self.retries_used:
            msg += f" [retries={self.retries_used}]"
        if self.error:
            msg += f" -- {self.error}"
        return msg


class ComputerUseTrajectory:
    """Append-only log of ``ComputerUseStep`` with a few conveniences."""

    def __init__(self, max_steps: int = 200) -> None:
        """Create an empty trajectory bounded to ``max_steps`` entries."""
        self.max_steps = max_steps
        self._steps: list[ComputerUseStep] = []

    def append(self, step: ComputerUseStep) -> None:
        """Append a step, evicting the oldest entries past ``max_steps``."""
        self._steps.append(step)
        if len(self._steps) > self.max_steps:
            # Drop oldest (they're rarely useful after the first few).
            del self._steps[: len(self._steps) - self.max_steps]

    def __iter__(self):
        return iter(self._steps)

    def __len__(self) -> int:
        return len(self._steps)

    def last(self) -> ComputerUseStep | None:
        """Return the most recent step, or None if empty."""
        return self._steps[-1] if self._steps else None

    def last_n(self, n: int) -> list[ComputerUseStep]:
        """Return the last ``n`` steps."""
        return self._steps[-n:]

    def recent_summary(self, n: int = 10) -> str:
        """Concatenated summary of the last ``n`` steps for prompt use."""
        if not self._steps:
            return "(no actions taken yet)"
        return "\n".join(
            f"  {i + 1}. {s.summary()}"
            for i, s in enumerate(self._steps[-n:
                ])
        )

    def to_dict(self) -> list[dict[str, Any]]:
        """Serialise every step to a list of JSON-friendly dictionaries."""
        return [
            {
                "action": s.action.to_dict(),
                "success": s.success,
                "result": s.result,
                "error": s.error,
                "elapsed_ms": s.elapsed_ms,
                "timestamp": s.timestamp,
            }
            for s in self._steps
        ]

    def recent_with_screenshots(self, n: int = 5) -> list[dict[str, Any]]:
        """Return the last ``n`` steps with their screenshots attached.

        This is what gets fed back into a VLM prompt so the model can
        see both *what* was just done and *what the screen looked like*
        after each step.  Falls back gracefully when a step has no
        screenshot (e.g. a typing action that didn't trigger a
        screenshot).
        """
        out: list[dict[str, Any]] = []
        for s in self._steps[-n:]:
            entry: dict[str, Any] = {
                "action": s.action.action,
                "target": s.action.target,
                "success": s.success,
                "summary": s.summary(),
                "elapsed_ms": s.elapsed_ms,
            }
            if s.screenshot_b64:
                entry["screenshot_b64"] = s.screenshot_b64
            if s.error:
                entry["error"] = s.error
            out.append(entry)
        return out

    def compress(self, strategy: str = "window",
                 keep_screenshots: int = 5) -> int:
        """Shrink the trajectory to bound context-window cost.

        Strategies
        ----------
        ``"window"`` (default)
            Keeps every step's *action text* but clears
            ``screenshot_b64`` for all but the most recent
            ``keep_screenshots`` steps.  VLM still gets the full action
            history; only the bulky screenshot payloads are dropped.

        Returns the number of steps whose screenshot was cleared.
        """
        if strategy != "window":
            raise ValueError(
                f"ComputerUseTrajectory.compress: unknown strategy "
                f"{strategy!r}; only 'window' is implemented"
            )
        if keep_screenshots < 0:
            raise ValueError(
                "ComputerUseTrajectory.compress: keep_screenshots must be >= 0"
            )
        cleared = 0
        # Steps that should KEEP their screenshot: the last
        # ``keep_screenshots`` ones that actually have one.
        keep_indices: set[int] = set()
        for idx in range(len(self._steps) - 1, -1, -1):
            if self._steps[idx].screenshot_b64 and len(keep_indices) < keep_screenshots:
                keep_indices.add(idx)
            elif len(keep_indices) >= keep_screenshots:
                break
        for idx, step in enumerate(self._steps):
            if idx in keep_indices or not step.screenshot_b64:
                continue
            step.screenshot_b64 = ""
            cleared += 1
        return cleared

    def total_screenshot_bytes(self) -> int:
        """Approximate in-memory cost of all stored screenshot payloads.

        Useful for telemetry / deciding when to call :meth:`compress`.
        """
        return sum(len(s.screenshot_b64) for s in self._steps)

    # ----- statistics -----

    def success_count(self) -> int:
        """Return the number of successful steps."""
        return sum(1 for s in self._steps if s.success)

    def failure_count(self) -> int:
        """Return the number of failed steps."""
        return sum(1 for s in self._steps if not s.success)

    def success_rate(self) -> float:
        """Fraction of successful steps in [0.0, 1.0].  0.0 if empty."""
        if not self._steps:
            return 0.0
        return self.success_count() / len(self._steps)

    def failure_breakdown(self) -> dict[str, int]:
        """Count of failures grouped by :class:`FailureKind` string.

        Unclassified / unannotated failures bucket under
        ``FailureKind.UNCLASSIFIED``.
        """
        out: dict[str, int] = Counter()
        for s in self._steps:
            if s.success:
                continue
            key = s.failure_kind or FailureKind.UNCLASSIFIED
            out[key] += 1
        return dict(out)

    def latency_stats(self) -> dict[str, float]:
        """Return ``p50``, ``p95``, ``mean``, ``min``, ``max`` latency in ms.

        Computed across all steps (including zero-elapsed ones, so
        screenshots and fast dispatches still count).
        """
        latencies = sorted(s.elapsed_ms for s in self._steps)
        if not latencies:
            return {
                "p50": 0.0, "p95": 0.0, "mean": 0.0,
                "min": 0.0, "max": 0.0, "count": 0,
            }
        n = len(latencies)
        mean = sum(latencies) / n

        def pct(p: float) -> float:
            idx = max(0, min(n - 1, round((p / 100.0) * (n - 1))))
            return float(latencies[idx])

        return {
            "p50": pct(50),
            "p95": pct(95),
            "mean": mean,
            "min": float(latencies[0]),
            "max": float(latencies[-1]),
            "count": n,
        }

    def page_diff_summary(self) -> dict[str, Any]:
        """Summary of pre/post-action state diffs.

        Counts how many steps verified that the page actually changed
        (i.e. ``page_changed is True``), how many observed no change
        (silent no-op), and how many didn't check.  Useful for
        spotting agents that are stuck clicking on dead elements.
        """
        changed = 0
        no_change = 0
        not_checked = 0
        for s in self._steps:
            if s.page_changed is True:
                changed += 1
            elif s.page_changed is False:
                no_change += 1
            else:
                not_checked += 1
        return {
            "changed": changed,
            "no_change": no_change,
            "not_checked": not_checked,
            "verified_change_rate": (
                (changed / (changed + no_change))
                if (changed + no_change) else 0.0
            ),
        }

    def retry_count(self) -> int:
        """Total additional retry attempts across all steps."""
        return sum(s.retries_used for s in self._steps)

    def fallback_count(self) -> int:
        """Number of steps that succeeded via a smart fallback."""
        return sum(1 for s in self._steps if s.fallback_used)

    def no_op_count(self) -> int:
        """Number of steps that produced a no-op (page didn't change)."""
        return sum(1 for s in self._steps if s.page_changed is False)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


#: Pattern used by :func:`_substitute_variables` to find ``{{key}}``
#: placeholders.  Captures the key name in group 1.
_VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


def _substitute_variables(
    payload: dict[str, Any], variables: dict[str, Any],
) -> dict[str, Any]:
    """Replace ``{{key}}`` placeholders in *payload* with values from
    *variables*.  Operates recursively on nested dicts and lists.
    String fields are processed with the regex; non-string fields are
    passed through unchanged.

    Missing keys are left in place (rather than raising) so a
    partial variable set still produces a useful preview.
    """
    def substitute_string(s: str) -> str:
        def repl(match: Any) -> str:
            key = match.group(1)
            if key in variables:
                v = variables[key]
                return str(v)
            return match.group(0)
        return _VAR_PATTERN.sub(repl, s)

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            return substitute_string(value)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(payload)


class EncreComputerUseSession:
    """Routes computer-use actions to either the browser or desktop backend.

    Lazily creates the underlying sessions on first use so a no-op
    ``await session.close()`` is safe even when the agent never
    actually drove the UI.
    """

    def __init__(
        self,
        default_target: str = "browser",
        max_steps: int = 200,
        browser_session: Any | None = None,
        desktop_session: Any | None = None,
        auto_screenshot: bool = True,
        retry_on_failure: int = 1,
        fallback_enabled: bool = True,
        no_op_as_failure: bool = True,
        cancel_check: Any | None = None,
        engine_requester: Any | None = None,
    ) -> None:
        """Construct a computer-use session.

        Parameters
        ----------
        default_target
            ``"browser"`` or ``"desktop"``; the fallback for actions
            that work on both (e.g. ``click``).
        max_steps
            Hard cap on the number of actions in the trajectory.
        browser_session, desktop_session
            Optional pre-built backend sessions (useful for tests /
            sharing a browser across sessions).
        auto_screenshot
            When True (default), every successful action that does
            *not* intrinsically take a screenshot is followed by a
            fresh screenshot capture, so the trajectory carries the
            post-action state.  This matches what Codex/Manus do.
        retry_on_failure
            Number of additional attempts when an action raises.
            ``1`` (default) means "try once, then retry once on
            failure".  A failed ``click`` is also retried with a
            ``find_text`` fallback when ``fallback_enabled`` is set.
        fallback_enabled
            When True, a failed ``click`` is followed by an
            automatic ``find_text`` lookup and re-click on the first
            match before declaring failure.
        no_op_as_failure
            When True (default), an action that returns ``success=True``
            but produces a post-state hash equal to the pre-state hash
            is reported as a ``NO_CHANGE`` failure so the agent's
            retry policy can kick in.  Set to False to revert to the
            "success + warning" behaviour.
        cancel_check
            Optional zero-arg callable returning ``True`` if the
            session should abort.  Checked at the start of each
            :meth:`dispatch`.  Also see :meth:`cancel`.
        engine_requester
            Optional async callable (see
            :class:`encre.computer.engine_bridge.EngineRequester`)
            invoked by :class:`EncreBrowserSession` when the bundled
            chromium binary is missing.  When set, the user is asked
            directly (via the desktop frontend) and the LLM is *not*
            involved.  When ``None`` (default), the original
            ``RuntimeError`` is raised so headless / server runs
            behave the same as before.
        """
        if default_target not in ("browser", "desktop"):
            raise ValueError("default_target must be 'browser' or 'desktop'")
        if retry_on_failure < 0:
            raise ValueError("retry_on_failure must be >= 0")
        self.default_target = default_target
        self.max_steps = max_steps
        self.trajectory = ComputerUseTrajectory(max_steps=max_steps)
        self._browser: Any = browser_session
        self._desktop: Any = desktop_session
        self._closed = False
        self._cancelled = False
        self.auto_screenshot = auto_screenshot
        self.retry_on_failure = retry_on_failure
        self.fallback_enabled = fallback_enabled
        self.no_op_as_failure = no_op_as_failure
        self._cancel_check = cancel_check
        # Per-failure-kind retry overrides.  Anything not present
        # here falls back to ``retry_on_failure`` + ``fallback_enabled``.
        self.retry_policy: dict[str, dict[str, Any]] = dict(
            DEFAULT_RETRY_POLICY
        )
        # Page / screen state hash cache.  Updated by the dispatcher's
        # ``expect_change`` path.  ``None`` means "no baseline yet".
        self._last_state_hash: str | None = None
        # Macro library: name -> list of action dicts.  Mutable so the
        # agent can register its own macros at runtime.
        self.macros: dict[str, list[dict[str, Any]]] = {}
        # Host hook for engine-install prompts.  Stored here so the
        # browser / desktop session can read it from inside their
        # ``_ensure_*`` paths, and so a caller can install / replace
        # it after construction via :meth:`set_engine_requester`.
        self._engine_requester = engine_requester

    def set_engine_requester(self, requester: Any | None) -> None:
        """Install / replace the engine-install requester.

        If the browser session was already lazily created the new
        requester is propagated to it as well; otherwise it will
        be picked up by the next :meth:`_ensure_browser` call.
        """
        self._engine_requester = requester
        if self._browser is not None and hasattr(
            self._browser, "set_engine_requester",
        ):
            self._browser.set_engine_requester(requester)

    def cancel(self) -> None:
        """Mark the session as cancelled.

        The flag is checked at the start of each :meth:`dispatch`; an
        in-flight action still runs to completion (we cannot safely
        abort a Playwright call mid-flight) but no new action will
        start after this is called.
        """
        self._cancelled = True

    @property
    def is_cancelled(self) -> bool:
        """True after :meth:`cancel` has been called."""
        return self._cancelled

    def _is_cancelled(self) -> bool:
        if self._cancelled:
            return True
        if self._cancel_check is not None:
            try:
                return bool(self._cancel_check())
            except Exception:
                logger.debug(
                    "computer-use: cancel_check raised; ignoring",
                    exc_info=True,
                )
        return False

    # ----- backend resolution -----

    def _ensure_browser(self) -> Any:
        """Return the browser backend, lazily creating it on first use."""
        if self._browser is None:
            from encre.computer.browser import EncreBrowserSession
            self._browser = EncreBrowserSession()
        if self._engine_requester is not None and hasattr(
            self._browser, "set_engine_requester",
        ):
            self._browser.set_engine_requester(self._engine_requester)
        return self._browser

    def _ensure_desktop(self) -> Any:
        """Return the desktop backend, lazily creating it on first use."""
        if self._desktop is None:
            from encre.computer.desktop import EncreDesktopSession
            self._desktop = EncreDesktopSession()
        return self._desktop

    # ----- dispatch -----

    async def _capture_fresh_screenshot(self, target: str) -> str:
        """Take a fresh screenshot from the appropriate backend.

        Returns the base64 PNG, or an empty string on failure (we
        never want a screenshot-capture glitch to fail the surrounding
        action).
        """
        try:
            if target == "browser":
                s = self._ensure_browser()
                data = await s.screenshot(full_page=False)
                return str(data)
            s = self._ensure_desktop()
            state = s.screenshot_with_cursor()
            return state.screenshot_b64
        except Exception as exc:
            logger.debug("computer-use: auto-screenshot failed: %s", exc)
            return ""

    async def _compute_state_hash(self, target: str) -> str:
        """Compute a short hash of the current DOM / screen state.

        Browser target: hash of the visible text + url.
        Desktop target: hash of the screenshot bytes.  Returns an
        empty string on failure (the caller treats that as "unknown,
        don't assert change").
        """
        try:
            if target == "browser":
                s = self._ensure_browser()
                # get_text is the cheap & fast option; if the page
                # is JS-rendered and that returns "", fall back to
                # get_all_text which is a full DOM dump.
                try:
                    text = await s.get_text("body")
                except Exception:
                    text = ""
                if not text:
                    try:
                        text = await s.get_all_text(50_000)
                    except Exception:
                        text = ""
                url = ""
                with contextlib.suppress(Exception):
                    url = await s.get_url()
                payload = f"{url}\n{text}".encode("utf-8", errors="ignore")
            else:
                s = self._ensure_desktop()
                png = s.take_screenshot_png()
                payload = bytes(png)
            if not payload:
                return ""
            return hashlib.sha1(payload).hexdigest()[:16]
        except Exception as exc:
            logger.debug("computer-use: state hash failed: %s", exc)
            return ""

    async def _dry_run_report(self, a: ComputerUseAction) -> dict[str, Any]:
        """Build a preview report for a dry-run action.

        Doesn't touch the backend; just describes *what would happen*
        and resolves coordinates where possible (so the agent can
        decide if the click target is sane).
        """
        report: dict[str, Any] = {
            "dry_run": True,
            "action": a.action,
            "target": a.target,
            "would_send": a.to_dict(),
        }
        try:
            if a.target == "browser" and a.query:
                s = self._ensure_browser()
                elements = await s.get_by_text_count(a.query, exact=a.exact)
                report["text_match_count"] = elements
            elif a.target == "browser" and a.selector:
                # Resolve the selector via wait_for_selector with a
                # very short timeout so dry-run never blocks the
                # agent.  A True result means the element is present
                # in the current DOM; False means it isn't (yet).
                s = self._ensure_browser()
                try:
                    present = await s.wait_for_selector(
                        a.selector, timeout=50,
                    )
                    report["selector_present"] = bool(present)
                except Exception:
                    report["selector_present"] = False
        except Exception as exc:
            report["dry_run_warning"] = str(exc)
        return report

    async def dispatch(self, action_input: dict[str, Any] | ComputerUseAction) -> dict[str, Any]:
        """Dispatch a single action.

        Returns a dict with at least ``success``, ``action``, and
        ``target``; on success also ``result`` (free-form), possibly
        ``screenshot_b64``; on failure also ``error`` and ``retries``.

        The return shape is the multimodal "everything the agent
        needs in one envelope" format used by Codex/Manus-style
        drivers: ``{success, action, target, result, screenshot_b64,
        retries_used, fallback_used, elapsed_ms, error}``.
        """
        try:
            action = (action_input
                      if isinstance(action_input, ComputerUseAction)
                      else ComputerUseAction.from_dict(action_input))
        except (TypeError, ValueError) as exc:
            return {
                "success": False,
                "action": None,
                "target": None,
                "error": str(exc),
            }
        if self._closed:
            return {
                "success": False,
                "action": action.action,
                "target": action.target,
                "error": "session is closed",
            }
        if self._is_cancelled():
            return {
                "success": False,
                "action": action.action,
                "target": action.target,
                "error": "session is cancelled",
            }
        # Dry-run: never touch the backend, just describe intent.
        if action.dry_run:
            report = await self._dry_run_report(action)
            return {
                "success": True,
                "action": action.action,
                "target": action.target,
                "result": report,
                "screenshot_b64": "",
                "retries_used": 0,
                "fallback_used": False,
                "elapsed_ms": 0,
                "dry_run": True,
            }

        start = time.time()
        screenshot_b64 = ""
        result: Any = None
        last_exc: Exception | None = None
        retries_used = 0
        fallback_used = False
        # Failure-aware retry:  after the first failure, consult
        # ``retry_policy`` to decide whether more retries make sense
        # *and* whether to swap in a different recovery action.  We
        # only run the smarter policy for the first failed attempt;
        # later retries just re-execute the original action.
        attempt = 0
        total_attempts = 1 + max(0, int(self.retry_on_failure))
        pre_hash = ""
        post_hash = ""
        page_changed: bool | None = None
        # If the action asks for a state change, snapshot the DOM
        # *now* so we can compare after.
        if action.expect_change:
            pre_hash = await self._compute_state_hash(action.target)
        while attempt < total_attempts:
            try:
                if attempt == 0 and self.fallback_enabled:
                    # Decide fallback kind based on retry_policy for
                    # the predicted failure mode.  We don't know the
                    # kind yet, so use the original action.
                    pass
                if action.target == "browser":
                    self._ensure_browser()
                    result, screenshot_b64 = await self._dispatch_browser(action)
                else:
                    result, screenshot_b64 = await self._dispatch_desktop(action)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                kind = classify_failure(exc)
                policy = self.retry_policy.get(
                    kind, self.retry_policy[FailureKind.UNCLASSIFIED],
                )
                if attempt == 0 and policy.get("fallback") == "find_text" \
                        and action.action == "click" \
                        and action.target == "browser" \
                        and (action.selector or action.query):
                    # Try the smarter find_text fallback ONCE before
                    # giving up.  This is what Codex/Manus do for
                    # "element not found" -- re-resolve via text.
                    try:
                        fb_action = ComputerUseAction(
                            action="click",
                            target="browser",
                            query=action.query or "",
                            selector="",
                            exact=action.exact,
                            fuzzy=action.fuzzy,
                            occurrence=action.occurrence,
                        )
                        self._ensure_browser()
                        result, screenshot_b64 = await self._dispatch_browser(
                            fb_action,
                        )
                        last_exc = None
                        fallback_used = True
                        logger.info(
                            "ComputerUse: find_text fallback for %s succeeded",
                            kind,
                        )
                        break
                    except Exception as fb_exc:
                        logger.info(
                            "ComputerUse: find_text fallback for %s also "
                            "failed: %s",
                            kind, fb_exc,
                        )
                        # Don't count the fallback attempt as a normal
                        # retry; let the regular retry loop continue.
                logger.warning(
                    "ComputerUse dispatch attempt %d/%d failed [%s]: %s",
                    attempt + 1, total_attempts, kind, exc, exc_info=True,
                )
                retries_used = attempt + 1
                # Per-kind cap: don't retry more than the policy says.
                if attempt + 1 >= max(
                    1, int(policy.get("retries", self.retry_on_failure))
                ):
                    break
                attempt += 1
        # Verify "expect_change" by hashing the post-state and
        # comparing to the pre-state.  Only meaningful for successful
        # actions; for failures we leave page_changed as None.
        if action.expect_change and last_exc is None and pre_hash:
            post_hash = await self._compute_state_hash(action.target)
            page_changed = (post_hash != pre_hash)
            # Cache the post hash as the new baseline for the next
            # ``expect_change`` action.
            if post_hash:
                self._last_state_hash = post_hash
        elif pre_hash and not action.expect_change:
            # Update baseline opportunistically so the *next* expect_change
            # has something to compare against.
            self._last_state_hash = pre_hash
        elapsed_ms = int((time.time() - start) * 1000)
        if last_exc is not None:
            kind = classify_failure(last_exc)
            step = ComputerUseStep(
                action=action, success=False, error=str(last_exc),
                elapsed_ms=elapsed_ms,
                failure_kind=kind,
                retries_used=retries_used,
                fallback_used=fallback_used,
            )
            self.trajectory.append(step)
            return {
                "success": False,
                "action": action.action,
                "target": action.target,
                "error": str(last_exc),
                "failure_kind": kind,
                "retries_used": retries_used,
                "fallback_used": fallback_used,
                "elapsed_ms": elapsed_ms,
            }
        # no-op detection: action returned ``success=True`` but the
        # post-state hash equals the pre-state hash.  When
        # ``no_op_as_failure`` is enabled (the flagship default) we
        # surface this as a NO_CHANGE failure (not a plain success)
        # so the trajectory sees it and the retry policy can kick in
        # next time.  When disabled, we keep the legacy "success +
        # warning" behaviour.
        no_op_detected = (
            action.expect_change
            and page_changed is False
            and post_hash is not None
        )
        if no_op_detected and self.no_op_as_failure:
            kind = FailureKind.NO_CHANGE
            step = ComputerUseStep(
                action=action, success=False,
                error=(
                    "action returned success but the DOM / screen state "
                    "is identical to before the action -- the click "
                    "may have hit a dead element"
                ),
                elapsed_ms=elapsed_ms,
                failure_kind=kind,
                page_changed=False,
                pre_hash=pre_hash,
                post_hash=post_hash,
                retries_used=retries_used,
                fallback_used=fallback_used,
            )
            self.trajectory.append(step)
            return {
                "success": False,
                "action": action.action,
                "target": action.target,
                "error": step.error,
                "failure_kind": kind,
                "page_changed": False,
                "no_op": True,
                "retries_used": retries_used,
                "fallback_used": fallback_used,
                "elapsed_ms": elapsed_ms,
            }
        # Successful path: optionally attach a fresh screenshot of the
        # *post*-action state, so the VLM sees what the world looks
        # like now rather than the pre-action frame.
        if self.auto_screenshot and not screenshot_b64:
            screenshot_b64 = await self._capture_fresh_screenshot(action.target)
        step = ComputerUseStep(
            action=action, success=True, result=result,
            elapsed_ms=elapsed_ms,
            screenshot_b64=screenshot_b64,
            page_changed=page_changed,
            pre_hash=pre_hash,
            post_hash=post_hash,
            retries_used=retries_used,
            fallback_used=fallback_used,
        )
        self.trajectory.append(step)
        out: dict[str, Any] = {
            "success": True,
            "action": action.action,
            "target": action.target,
            "result": result,
            "retries_used": retries_used,
            "fallback_used": fallback_used,
            "elapsed_ms": elapsed_ms,
        }
        if screenshot_b64:
            out["screenshot_b64"] = screenshot_b64
        if action.expect_change and page_changed is not None:
            out["page_changed"] = page_changed
            if not page_changed:
                out["warning"] = (
                    "action returned success but the DOM / screen state "
                    "is identical to before the action -- the click "
                    "may have hit a dead element"
                )
        return out

    # ----- browser backend -----

    async def _dispatch_browser(self, a: ComputerUseAction) -> tuple[Any, str]:
        """Route a normalised action to the browser backend.

        Returns a ``(result, screenshot_b64)`` tuple. Raises ``ValueError``
        for missing arguments or unsupported browser actions.
        """
        s = self._ensure_browser()
        act = a.action
        if act == "navigate":
            url = a.text or a.extras.get("url", "")
            if not url:
                raise ValueError("navigate: 'text' or 'url' is required")
            state = await s.navigate(url)
            return {"url": state.url, "title": state.title}, ""
        if act == "go_back":
            return {"success": await s.go_back()}, ""
        if act == "go_forward":
            return {"success": await s.go_forward()}, ""
        if act == "reload":
            return {"success": await s.reload()}, ""
        if act == "get_url":
            return {"url": await s.get_url()}, ""
        if act == "get_title":
            return {"title": await s.get_title()}, ""
        if act == "get_html":
            return {"html": await s.get_html()}, ""
        if act == "get_text":
            return {"text": await s.get_text(a.selector or None)}, ""
        if act == "get_all_text":
            return {"text": await s.get_all_text(int(a.extras.get("max_chars", 200000)))}, ""
        if act == "get_attribute":
            if not a.selector or not a.attr_name:
                raise ValueError("get_attribute: selector and attr_name required")
            return {"value": await s.get_attribute(a.selector, a.attr_name)}, ""
        if act == "get_property":
            if not a.selector or not a.attr_name:
                raise ValueError("get_property: selector and attr_name required")
            return {"value": await s.get_property(a.selector, a.attr_name)}, ""
        if act == "execute_js":
            if not a.js_code:
                raise ValueError("execute_js: 'js_code' (or 'code') is required")
            return {"result": await s.execute_js(a.js_code)}, ""
        if act == "select_option":
            if not a.selector:
                raise ValueError("select_option: selector required")
            ok = await s.select_option(a.selector, a.option_value, by=a.option_by)
            return {"success": ok}, ""
        if act == "scroll_page":
            await s.scroll_to(int(a.x), int(a.y))
            return {"action": "scroll_page", "x": int(a.x), "y": int(a.y)}, ""
        if act == "list_tabs":
            return {"tabs": await s.list_tabs()}, ""
        if act == "switch_tab":
            return {"success": await s.switch_tab(a.tab_index)}, ""
        if act == "new_tab":
            url = a.text or a.extras.get("url") or None
            return await s.new_tab(url=url), ""
        if act == "close_tab":
            return {"success": await s.close_tab(a.tab_index if a.tab_index >= 0 else None)}, ""
        if act == "set_dialog_handler":
            await s.set_dialog_handler(accept=a.accept, prompt_text=a.prompt_text)
            return {"action": "set_dialog_handler", "accept": a.accept}, ""
        if act == "set_file_chooser_handler":
            if not a.paths:
                raise ValueError("set_file_chooser_handler: 'paths' required")
            await s.set_file_chooser_handler(a.paths)
            return {"action": "set_file_chooser_handler", "files": len(a.paths)}, ""
        if act == "a11y_snapshot":
            return await s.a11y_snapshot(
                interesting_only=bool(a.extras.get("interesting_only", True)),
                root_selector=a.extras.get("root_selector") or None,
            ), ""
        if act == "click_by_role":
            role = a.extras.get("role", "")
            name = a.query or a.attr_name
            if not role or not name:
                raise ValueError("click_by_role: 'role' and 'name' required")
            return {"success": await s.click_by_role(role, name, exact=a.exact)}, ""
        if act == "get_by_text_count":
            text = a.query or a.text
            return {"count": await s.get_by_text_count(text, exact=a.exact)}, ""
        if act == "get_page_structure":
            return await s.get_page_structure(), ""
        if act == "fill_form":
            if not a.fields:
                raise ValueError("fill_form: 'fields' required")
            return {"success": await s.fill_form(a.fields)}, ""
        # Cross-target actions below
        if act == "screenshot":
            data = await s.screenshot(
                full_page=bool(a.extras.get("full_page", False)),
                selector=a.selector or None,
            )
            return {"screenshot_b64": data}, data
        if act == "screenshot_viewport":
            info = await s.screenshot_viewport()
            return info, info.get("screenshot_base64", "")
        if act == "click":
            if a.selector:
                return {"success": await s.click(a.selector)}, ""
            if a.query:
                return {"success": await s.click_text(a.query, fuzzy=a.fuzzy,
                                                     occurrence=a.occurrence,
                                                     exact=a.exact)}, ""
            return {"success": await s.click_at(a.x, a.y)}, ""
        if act == "double_click":
            return {"success": await s.double_click_at(a.x, a.y)}, ""
        if act == "right_click":
            return {"success": await s.right_click_at(a.x, a.y)}, ""
        if act == "click_at":
            return {"success": await s.click_at(a.x, a.y)}, ""
        if act == "double_click_at":
            return {"success": await s.double_click_at(a.x, a.y)}, ""
        if act == "right_click_at":
            return {"success": await s.right_click_at(a.x, a.y)}, ""
        if act == "hover":
            if a.selector:
                return {"success": await s.hover(a.selector)}, ""
            return {"success": await s.hover_at(a.x, a.y)}, ""
        if act == "hover_at":
            return {"success": await s.hover_at(a.x, a.y)}, ""
        if act == "type":
            if a.selector:
                return {"success": await s.type_text(a.selector, a.text)}, ""
            return {"success": await s.type_at(a.x, a.y, a.text)}, ""
        if act == "type_at":
            return {"success": await s.type_at(a.x, a.y, a.text)}, ""
        if act == "press_key":
            if not a.key:
                raise ValueError("press_key: 'key' required")
            await s.press_key(a.key)
            return {"action": "press_key", "key": a.key}, ""
        if act == "key":
            if not a.key:
                raise ValueError("key: 'key' required")
            await s.press_key(a.key)
            return {"action": "key", "key": a.key}, ""
        if act == "hotkey":
            if not a.keys:
                raise ValueError("hotkey: 'keys' required")
            return {"success": await s.hotkey(a.keys)}, ""
        if act == "scroll":
            if a.x or a.y:
                await s.scroll_to(int(a.x), int(a.y))
                return {"action": "scroll_to", "x": int(a.x), "y": int(a.y)}, ""
            return {"action": "scroll", "amount": a.scroll_amount}, ""
        if act == "drag":
            return {"success": await s.drag(a.x, a.y, a.x2, a.y2)}, ""
        if act == "move_mouse":
            return {"success": await s.move_mouse(a.x, a.y)}, ""
        if act == "click_text":
            if not a.query:
                raise ValueError("click_text: 'query' (or 'text') required")
            return {"success": await s.click_text(a.query, fuzzy=a.fuzzy,
                                                 occurrence=a.occurrence,
                                                 exact=a.exact)}, ""
        if act == "find_text":
            if not a.query:
                raise ValueError("find_text: 'query' (or 'text') required")
            return await s.find_text(a.query, fuzzy=a.fuzzy, occurrence=a.occurrence,
                                     exact=a.exact), ""
        if act == "wait":
            if a.ms < 0:
                raise ValueError("wait: ms must be >= 0")
            await s.wait(a.ms)
            return {"action": "wait", "ms": a.ms}, ""
        if act == "done":
            return {"action": "done"}, ""
        raise ValueError(f"unsupported browser action: {act!r}")

    # ----- desktop backend -----

    async def _dispatch_desktop(self, a: ComputerUseAction) -> tuple[Any, str]:
        """Route a normalised action to the desktop backend.

        Returns a ``(result, screenshot_b64)`` tuple. Raises ``ValueError``
        for missing arguments or unsupported desktop actions.
        """
        s = self._ensure_desktop()
        act = a.action
        # Map cross-target actions to their desktop equivalent.
        if act == "click":
            if a.query:
                res = s.click_text(a.query, fuzzy=a.fuzzy, occurrence=a.occurrence,
                                   button=a.button, coord_space=a.coord_space)
                return res, ""
            if a.x or a.y:
                return s.click(x=a.x or None, y=a.y or None,
                               button=a.button, coord_space=a.coord_space), ""
            return s.click(button=a.button, coord_space=a.coord_space), ""
        if act == "double_click":
            return s.double_click(x=a.x or None, y=a.y or None,
                                  coord_space=a.coord_space), ""
        if act == "right_click":
            return s.right_click(x=a.x or None, y=a.y or None,
                                 coord_space=a.coord_space), ""
        if act == "triple_click":
            return s.triple_click(x=a.x or None, y=a.y or None,
                                  coord_space=a.coord_space), ""
        if act == "type":
            return s.type_text(a.text), ""
        if act == "press_key" or act == "key":
            if not a.key:
                raise ValueError("press_key: 'key' required")
            return s.press_key(a.key), ""
        if act == "hotkey":
            if not a.keys:
                raise ValueError("hotkey: 'keys' required")
            return s.hotkey(a.keys), ""
        if act == "scroll":
            return s.scroll(a.scroll_amount, x=a.x or None, y=a.y or None), ""
        if act == "drag":
            return s.drag(a.x, a.y, a.x2, a.y2, coord_space=a.coord_space), ""
        if act == "move_mouse":
            return s.move_mouse(a.x, a.y, coord_space=a.coord_space), ""
        if act == "hover":
            return s.move_mouse(a.x, a.y, coord_space=a.coord_space), ""
        if act == "hover_at":
            return s.move_mouse(a.x, a.y, coord_space=a.coord_space), ""
        if act == "screenshot":
            state = s.screenshot_with_cursor()
            payload = {
                "width": state.width,
                "height": state.height,
                "logical_width": state.logical_width,
                "logical_height": state.logical_height,
                "dpi_scale_x": state.dpi_scale_x,
                "dpi_scale_y": state.dpi_scale_y,
                "cursor_x": state.cursor_x,
                "cursor_y": state.cursor_y,
                "screenshot_b64": state.screenshot_b64,
            }
            return payload, state.screenshot_b64
        if act == "wait":
            if a.ms < 0:
                raise ValueError("wait: ms must be >= 0")
            return s.wait(a.ms), ""
        if act == "done":
            return {"action": "done"}, ""
        if act == "click_text":
            if not a.query:
                raise ValueError("click_text: 'query' (or 'text') required")
            return s.click_text(a.query, fuzzy=a.fuzzy, occurrence=a.occurrence,
                                button=a.button, coord_space=a.coord_space), ""
        if act == "find_text":
            if not a.query:
                raise ValueError("find_text: 'query' (or 'text') required")
            return s.find_text(a.query, fuzzy=a.fuzzy, occurrence=a.occurrence), ""
        if act == "clipboard_get":
            return {"text": s.clipboard_get()}, ""
        if act == "clipboard_set":
            return s.clipboard_set(a.text), ""
        if act == "file_drop":
            if not a.paths:
                raise ValueError("file_drop: 'paths' required")
            return s.file_drop(a.x, a.y, a.paths, coord_space=a.coord_space), ""
        if act == "get_screen_size":
            return s.get_screen_size(), ""
        if act == "get_cursor_position":
            return s.get_cursor_position(), ""
        if act == "accessibility_tree":
            return s.accessibility_tree(
                max_depth=int(a.extras.get("max_depth", 6)),
                max_nodes=int(a.extras.get("max_nodes", 500)),
            ), ""
        if act == "find_element_by_name":
            control = a.extras.get("control_type") or None
            return s.find_element_by_name(a.query or a.attr_name,
                                          control_type=control), ""
        if act == "get_elements":
            from encre.computer.ocr import ocr_image
            prefer_window = bool(a.extras.get("prefer_active_window", True))
            window_capture = (
                s._capture_active_window_png() if prefer_window else None
            )
            used_window = window_capture is not None
            if used_window:
                img_bytes = window_capture.png_bytes
                off_x = int(window_capture.left)
                off_y = int(window_capture.top)
            else:
                state = s.screenshot_with_cursor()
                img_bytes = base64.b64decode(state.screenshot_b64)
                off_x = 0
                off_y = 0
            elements = ocr_image(img_bytes)
            if used_window:
                # Translate OCR bbox from window-local coords to screen
                # coords so click_text / find_text keep working.
                for e in elements:
                    e["x"] = int(e["x"]) + off_x
                    e["y"] = int(e["y"]) + off_y
                    e["center_x"] = int(e["center_x"]) + off_x
                    e["center_y"] = int(e["center_y"]) + off_y
            # Attach the screenshot to the trajectory so the model can
            # actually see the desktop.  When the foreground-window
            # capture succeeded we also include its bitmap (DirectX /
            # Direct2D content, occluded regions) so the model can read
            # pixels the OS compositor stripped from the desktop shot.
            state = s.screenshot_with_cursor()
            payload: dict[str, Any] = {
                "elements": elements,
                "screen_width": state.width,
                "screen_height": state.height,
                "logical_width": state.logical_width,
                "logical_height": state.logical_height,
                "dpi_scale_x": state.dpi_scale_x,
                "dpi_scale_y": state.dpi_scale_y,
                "cursor_x": state.cursor_x,
                "cursor_y": state.cursor_y,
                "ocr_source": "active_window" if used_window else "desktop",
            }
            if used_window:
                payload["window_screenshot_base64"] = (
                    base64.b64encode(window_capture.png_bytes).decode("ascii")
                )
                payload["window_left"] = int(window_capture.left)
                payload["window_top"] = int(window_capture.top)
                payload["window_width"] = int(window_capture.width)
                payload["window_height"] = int(window_capture.height)
            return payload, state.screenshot_b64
        if act == "take_screenshot_png":
            png = s.take_screenshot_png()
            return {"screenshot_b64": base64.b64encode(png).decode("ascii")}, \
                base64.b64encode(png).decode("ascii")
        if act == "locate_on_screen":
            template = a.extras.get("template", "")
            if not template:
                raise ValueError("locate_on_screen: 'template' (base64 PNG) required")
            confidence = float(a.extras.get("confidence", 0.9))
            return s.locate_on_screen(template, confidence=confidence), ""
        if act == "fill_form":
            raise ValueError("fill_form is browser-only")
        if act == "navigate":
            raise ValueError("navigate is browser-only")
        raise ValueError(f"unsupported desktop action: {act!r}")

    # ----- lifecycle -----

    # ----- session lifecycle -----

    async def close(self) -> None:
        """Close the session and its browser backend (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.debug("computer-use: browser close failed", exc_info=True)
        # desktop has no close; nothing to do.

    def trajectory_dict(self) -> list[dict[str, Any]]:
        """Return the trajectory serialised as a list of step dictionaries."""
        return self.trajectory.to_dict()

    # ----- state checkpoint / restore -----

    async def save_state(self) -> dict[str, Any]:
        """Snapshot everything needed to resume a session later.

        The browser target returns cookies, local storage and the
        current URL.  The desktop target returns a thumbnail
        screenshot of the current screen.  The trajectory is *not*
        included -- caller can stash it separately if they want.
        """
        state: dict[str, Any] = {
            "target": self.default_target,
            "step_count": len(self.trajectory._steps),
        }
        try:
            if self._browser is not None:
                cookies = await self._browser.save_cookies()
                state["browser_cookies"] = cookies
                storage: dict[str, str] = {}
                try:
                    storage = await self._browser.get_local_storage() or {}
                except Exception:
                    storage = {}
                state["browser_local_storage"] = storage
                with contextlib.suppress(Exception):
                    state["browser_url"] = await self._browser.get_url()
        except Exception as exc:
            logger.info("save_state: browser section failed: %s", exc)
        try:
            if self._desktop is not None:
                with contextlib.suppress(Exception):
                    state["desktop_screenshot_png"] = (
                        self._desktop.take_screenshot_png()
                    )
        except Exception as exc:
            logger.info("save_state: desktop section failed: %s", exc)
        return state

    async def load_state(self, state: dict[str, Any]) -> None:
        """Restore a session from a :meth:`save_state` snapshot.

        Best-effort: missing keys are skipped, partial restoration
        is allowed.  Used by ``replay`` and by agents that need to
        "rewind" after a wrong turn.
        """
        if not isinstance(state, dict):
            raise TypeError("load_state: state must be a dict")
        if self._browser is not None:
            cookies = state.get("browser_cookies")
            if cookies:
                try:
                    await self._browser.load_cookies(cookies)
                except Exception as exc:
                    logger.info(
                        "load_state: load_cookies failed: %s", exc,
                    )
            url = state.get("browser_url")
            if url and isinstance(url, str) and url.startswith(("http://", "https://")):
                try:
                    await self._browser.navigate(url)
                except Exception as exc:
                    logger.info(
                        "load_state: navigate(%r) failed: %s", url, exc,
                    )
        # Local-storage restoration needs evaluate_js; we use a
        # small JS snippet to set every key.
        if self._browser is not None and state.get("browser_local_storage"):
            try:
                entries = state["browser_local_storage"]
                if isinstance(entries, dict) and entries:
                    js = (
                        "(function(){"
                        "var d=" + str(dict(entries)).replace(
                            "'", "\\'",
                        ) + ";"
                        "try{for(var k in d){localStorage.setItem(k,d[k]);}}"
                        "catch(e){}"
                        "return true;"
                        "})()"
                    )
                    await self._browser.evaluate_js(js)
            except Exception as exc:
                logger.info(
                    "load_state: local_storage restore failed: %s", exc,
                )

    # ----- macro actions -----

    def register_macro(self, name: str, actions: list[dict[str, Any]]) -> None:
        """Register a named sequence of actions for later execution.

        The actions are stored as plain dicts (the same shape the
        tool layer uses) so they can be registered from JSON.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("register_macro: name must be a non-empty string")
        if not isinstance(actions, list) or not all(
            isinstance(x, dict) for x in actions
        ):
            raise TypeError("register_macro: actions must be list[dict]")
        self.macros[name] = list(actions)

    def unregister_macro(self, name: str) -> bool:
        """Remove a macro; returns True if it existed."""
        return self.macros.pop(name, None) is not None

    async def execute_macro(
        self, name: str, *, stop_on_failure: bool = True,
    ) -> list[dict[str, Any]]:
        """Run a registered macro; returns per-step results.

        Each sub-action is dispatched through the normal pipeline
        (so retries, fallback, and auto-screenshot all still work).
        If ``stop_on_failure`` is True (default), execution halts
        on the first failed step and remaining steps are returned
        with ``success=False, error="skipped"``.
        """
        if name not in self.macros:
            raise KeyError(f"execute_macro: unknown macro {name!r}")
        results: list[dict[str, Any]] = []
        for i, action in enumerate(list(self.macros[name])):
            try:
                r = await self.dispatch(action)
            except Exception as exc:
                r = {
                    "success": False,
                    "action": action.get("action"),
                    "target": action.get("target"),
                    "error": str(exc),
                }
            results.append(r)
            if stop_on_failure and not r.get("success"):
                # Pad the remaining steps with skipped results.
                for j in range(i + 1, len(self.macros[name])):
                    results.append({
                        "success": False,
                        "action": self.macros[name][j].get("action"),
                        "target": self.macros[name][j].get("target"),
                        "error": "skipped (prior step failed)",
                    })
                break
        return results

    # ----- metrics / telemetry -----

    def metrics(self) -> dict[str, Any]:
        """Return a snapshot of the session's usage metrics.

        The output is the same shape Codex/Manus-style observability
        expects: success rate, latency percentiles, failure-kind
        histogram, page-change verification rate, and totals.
        """
        t = self.trajectory
        return {
            "steps": len(t._steps),
            "success_count": t.success_count(),
            "failure_count": t.failure_count(),
            "no_op_count": t.no_op_count(),
            "success_rate": t.success_rate(),
            "latency": t.latency_stats(),
            "failure_breakdown": t.failure_breakdown(),
            "page_diff": t.page_diff_summary(),
            "retry_count": t.retry_count(),
            "fallback_count": t.fallback_count(),
            "total_screenshot_bytes": t.total_screenshot_bytes(),
            "macros": sorted(self.macros.keys()),
        }

    # ----- macro library (per-session handle) -----

    def ensure_macro_library(self) -> MacroLibrary:
        """Return a process-wide :class:`MacroLibrary`, lazily created.

        The first call instantiates a :class:`MacroLibrary` rooted at
        :data:`DEFAULT_MACRO_LIBRARY_PATH` and stashes it on the
        session.  Subsequent calls return the same instance, so
        macros registered from one meta-action are visible to the
        next.
        """
        existing = getattr(self, "_macro_library", None)
        if existing is None:
            existing = MacroLibrary()
            self._macro_library = existing
        return existing

    @property
    def macro_library(self) -> MacroLibrary:
        """The lazy :class:`MacroLibrary` associated with this session."""
        return self.ensure_macro_library()

    # ----- cross-session export / replay -----

    async def export_bundle(
        self, *, include_state: bool = True, source: str = "",
    ) -> dict[str, Any]:
        """Export a self-contained bundle of the session.

        The bundle carries the full action list (suitable for
        :meth:`replay` / :meth:`replay_bundle`) plus optional
        browser state, environment metadata, and the in-session
        macro library so a different process can resume the work
        without losing context.

        Use ``source`` to label the producer (e.g. an agent
        identifier) -- it's purely informational.
        """
        bundle: dict[str, Any] = {
            "schema": 1,
            "source": source,
            "exported_at": time.time(),
            "default_target": self.default_target,
            "max_steps": self.max_steps,
            "actions": [step.action.to_dict() for step in self.trajectory._steps],
            "step_count": len(self.trajectory._steps),
            "metrics": self.metrics(),
            "in_session_macros": {
                name: list(actions)
                for name, actions in self.macros.items()
            },
        }
        if include_state:
            try:
                bundle["state"] = await self.save_state()
            except Exception as exc:
                logger.info(
                    "export_bundle: save_state failed: %s", exc,
                )
        return bundle

    async def replay_bundle(
        self,
        bundle: dict[str, Any],
        *,
        start: int = 0,
        stop_on_failure: bool = True,
        variables: dict[str, Any] | None = None,
        restore_state: bool = True,
    ) -> list[dict[str, Any]]:
        """Replay a bundle exported via :meth:`export_bundle`.

        Steps are normalised back into action dicts, then
        dispatched in order.  The optional ``variables`` mapping
        substitutes ``{{key}}`` placeholders in the action payloads
        (so the same bundle can be replayed in different
        environments by swapping the username, URL, etc.).

        If ``restore_state`` is True (default) the browser state
        from the bundle is loaded before the first replay step.
        """
        if not isinstance(bundle, dict):
            raise TypeError("replay_bundle: bundle must be a dict")
        if restore_state and isinstance(bundle.get("state"), dict):
            try:
                await self.load_state(bundle["state"])
            except Exception as exc:
                logger.info(
                    "replay_bundle: load_state failed: %s", exc,
                )
        raw_actions = bundle.get("actions") or []
        if not isinstance(raw_actions, list):
            raise TypeError("replay_bundle: bundle.actions must be a list")
        # Pre-merge any in-session macros from the bundle so actions
        # that reference them (e.g. via the meta-action executor)
        # have something to find.
        macros = bundle.get("in_session_macros") or {}
        if isinstance(macros, dict):
            for name, actions in macros.items():
                if isinstance(name, str) and isinstance(actions, list):
                    self.macros.setdefault(name, list(actions))
        return await self.replay(
            raw_actions, start=start, stop_on_failure=stop_on_failure,
            variables=variables,
        )

    # ----- parameterised replay -----

    async def replay(
        self,
        steps: list[dict[str, Any]] | list[ComputerUseStep],
        *,
        start: int = 0,
        stop_on_failure: bool = True,
        variables: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Re-execute a recorded sequence of actions.

        Each input is either a list of :class:`ComputerUseStep`
        (e.g. ``session.trajectory._steps``) or a list of action
        dicts (e.g. a saved JSON trajectory).  Use ``start`` to skip
        a prefix -- handy when the first N steps were "setup" you
        don't want to repeat.

        The optional ``variables`` mapping is used to substitute
        ``{{key}}`` placeholders in string-valued action fields
        (e.g. ``text``, ``query``, ``selector``).  This lets the
        same recorded sequence run in different environments by
        swapping the URL, username, etc.
        """
        out: list[dict[str, Any]] = []
        # Normalise inputs to action dicts.
        normalised: list[dict[str, Any]] = []
        for s in steps:
            if isinstance(s, ComputerUseStep):
                normalised.append(s.action.to_dict())
            elif isinstance(s, dict) and "action" in s:
                # Could be either an action dict or a step dict with
                # nested "action".  Be lenient.
                if isinstance(s["action"], dict):
                    normalised.append(s["action"])
                else:
                    normalised.append(s)
            elif isinstance(s, dict):
                normalised.append(s)
            else:
                raise TypeError(
                    f"replay: unexpected step type {type(s).__name__}"
                )
        for i in range(start, len(normalised)):
            payload = normalised[i]
            if variables:
                payload = _substitute_variables(payload, variables)
            try:
                r = await self.dispatch(payload)
            except Exception as exc:
                r = {
                    "success": False,
                    "action": payload.get("action"),
                    "target": payload.get("target"),
                    "error": str(exc),
                }
            out.append(r)
            if stop_on_failure and not r.get("success"):
                break
        return out


__all__ = [
    "DEFAULT_MACRO_LIBRARY_PATH",
    "DEFAULT_RETRY_POLICY",
    "VALID_ACTIONS",
    "ComputerUseAction",
    "ComputerUseStep",
    "ComputerUseTrajectory",
    "EncreComputerUseSession",
    "FailureKind",
    "MacroEntry",
    "MacroLibrary",
    "classify_failure",
]
