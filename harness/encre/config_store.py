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

"""Split, single-source configuration store for Encre.

Background
----------
Configuration used to live in **one** file -- ``model/config.toml`` -- which
despite its name held the entire :class:`~encre.config.EncreConfig` blob:
model endpoints, *agents*, *sub-agents*, every platform adapter's
credentials, MCP servers and all UI preferences.  A second, overlapping
store (``settings.json``) duplicated ten of those keys plus the adapter
credentials again, and won at runtime for the keys it held.  So the same
value had two homes that could -- and did -- disagree, and "the model
config file" was not a model config file at all.

Layout
------
Every file below is individually encrypted (AES-256-GCM via
:mod:`encre.crypto`) and holds exactly one responsibility:

======================  =====================================================
``config/models.json``  model endpoints, active model, fallback, slot budget
``config/agents.json``  agent personas, sub-agents, system prompt, skills
``config/adapters.json``platform adapter credentials (``adapter_*``)
``config/mcp.json``     MCP server definitions
``config/ui.json``      presentation prefs + the flat runtime overlay that
                        used to be the top-level ``settings.json``
``config/runtime.json``everything else (behaviour flags, permissions,
                        tracing, device context) -- also the catch-all
======================  =====================================================

Unknown/new fields land in ``runtime.json`` so adding a field to
:class:`~encre.config.EncreConfig` never silently drops data.

Compatibility
-------------
``EncreConfig.from_file()`` / ``EncreConfig.save()`` keep their exact
signatures and semantics; only the on-disk layout changes.  On first load
the legacy ``model/config.toml`` and ``settings.json`` are read and split
into the new files, then **renamed** (never deleted) to
``*.migrated-<timestamp>`` so the original bytes stay recoverable.
"""

import json
import os
import time
from pathlib import Path
from typing import Any

from encre.crypto import decrypt, encrypt
from encre.paths import get_data_dir


from encre.logging_config import get_logger


logger = get_logger(__name__)

__all__ = [
    "UI_SETTING_KEYS",
    "adapter_flat_view",
    "config_dir",
    "group_path",
    "is_legacy_config_path",
    "legacy_config_path",
    "legacy_settings_path",
    "load_config",
    "load_settings_file",
    "migrate_legacy",
    "overlay_view",
    "save_config",
    "save_group",
    "save_overlay",
    "save_settings_file",
]

# --------------------------------------------------------------------------
# Layout constants
# --------------------------------------------------------------------------

CONFIG_DIR_NAME = "config"

#: One file per responsibility.  ``runtime`` is the catch-all for any field
#: that is not explicitly claimed by another group.
GROUP_FILES: dict[str, str] = {
    "models": "models.json",
    "agents": "agents.json",
    "adapters": "adapters.json",
    "mcp": "mcp.json",
    "ui": "ui.json",
    "runtime": "runtime.json",
}

#: Marker written into each file so a future reader can tell a split file
#: from an unrelated JSON blob.
_SCHEMA_KEY = "_encre_config_schema"
_SCHEMA_VERSION = 2

#: Where the pre-split, do-everything config file lived.
_LEGACY_CONFIG_PARTS = ("model", "config.toml")
#: Where the pre-split flat runtime overlay lived.
_LEGACY_SETTINGS_NAME = "settings.json"


# --------------------------------------------------------------------------
# Group membership -- explicit, not inferred
# --------------------------------------------------------------------------

_MODEL_FIELDS = frozenset({
    "providers",
    # Legacy flat model list -- only read during migration; no longer written.
    "models",
    "active_model_index",
    "model",
    "api_key",
    "base_url",
    "max_tokens",
    "backend_type",
    "backend_kwargs",
    "target_model_ids",
    "fallback_model",
    "fallback_base_url",
    "fallback_api_key",
    "fallback_backend_type",
    "classifier_model",
    "default_slot_tokens",
    "thinking_config",
    "thinking_prefill_enabled",
    "enable_prompt_caching",
})

_AGENT_FIELDS = frozenset({
    "agents",
    "active_agent_index",
    "sub_agents",
    "system_prompt",
    "default_specialty",
    "enabled_skills",
    "current_tool_policy",
    "max_turns",
})

#: Presentation preferences.  This is the successor of the old top-level
#: ``settings.json``: the same flat key/value store the settings panel
#: writes through the WS ``configure`` path, so the semantics (last write
#: wins, values visible to ``load_settings()``) are preserved exactly.
UI_SETTING_KEYS = frozenset({
    "shortcut_send_mode",
    "language",
    "language_preference",
    "plan_mode_max_turns",
    "spec_mode_max_turns",
    "default_link_behavior",
    "auto_expand",
    "sub_agent_auto_open_view",
    "automation_auto_open_view",
    "startup_session_mode",
    "startup_session_behavior",
    "model_pool_enabled",
    "model_pool_fallback_enabled",
    "model_pool_cycle_order",
    "model_pool_cooldown_ms",
    "model_pool_recycle_wait_ms",
    "model_pool_max_fallbacks",
    "default_search_engine",
    "default_search_engine_url",
    "terminal_font_size",
    "terminal_cursor_blink",
    "terminal_scrollback",
    "terminal_font_family",
    "terminal_line_height",
    "terminal_cursor_style",
    "terminal_cursor_inactive",
    "terminal_bold_bright",
    "terminal_bell",
    "terminal_copy_on_select",
    "terminal_right_click",
    "terminal_cols_mode",
    "terminal_shell",
    "terminal_start_dir",
    "terminal_cursor_width", "terminal_font_weight", "terminal_letter_spacing",
    "terminal_min_contrast", "terminal_convert_eol", "terminal_word_separator",
    "editor_font_size",
    "editor_word_wrap",
    "editor_minimap",
    "editor_line_numbers",
    "editor_tab_size",
    "editor_detect_indentation",
    "editor_render_whitespace",
    "editor_cursor_blink",
    "editor_font_ligatures",
    "editor_sticky_scroll",
    "editor_auto_save",
    "editor_tab_indent",
    "editor_use_spaces",
    "editor_word_wrap_column",
    "editor_folding",
    "editor_glyph_margin",
    "editor_format_on_paste",
    "editor_rulers",
    "editor_cursor_style",
    "editor_cursor_blinking",
    "review_split_view",
    "review_collapse_unified",
    "review_wrap",
    "review_word_diff",
    "review_hide_whitespace",
    "review_full_file",
    "review_default_filter",
    "review_max_lines",
    "browser_homepage",
    "browser_record_history",
    "browser_do_not_track",
    "browser_deny_site_permissions",
    "browser_block_popups",
    "browser_zoom_percent",
    "browser_ask_before_download",
    "browser_download_dir",
    "browser_block_third_party_cookies",
    "browser_fresh_session",
    "browser_force_light_page",
    "browser_identity",
    "browser_history_limit",
    "device_context_enabled",
    "network_proxy_mode",
    "network_proxy_scheme",
    "network_proxy_host",
    "network_proxy_port",
    "network_proxy_username",
    "network_proxy_password",
    "network_proxy_bypass",
})

#: Adapter fields are stored/transported flattened as ``adapter_<id>_<field>``
#: (the shape the frontend protocol uses).
_ADAPTER_PREFIX = "adapter_"

#: Keys the flat overlay *surfaces* for legacy consumers but does not own --
#: they are written from the typed model into another group file.
_BORROWED_KEYS = frozenset({"permission_settings"})

#: Guard against the legacy overlay having written ``str(True) == "True"``
#: for a boolean field: coerce known keys back to their real type on both
#: read and write.  Keys absent from this map are passed through untouched.
_UI_KEY_TYPES: dict[str, type] = {
    "auto_expand": bool,
    "plan_mode_max_turns": int,
    "spec_mode_max_turns": int,
    "sub_agent_auto_open_view": bool,
    "automation_auto_open_view": bool,
    "model_pool_enabled": bool,
    "model_pool_fallback_enabled": bool,
    "model_pool_order_sequential": bool,
    "model_pool_cooldown_ms": int,
    "model_pool_recycle_wait_ms": int,
    "model_pool_max_fallbacks": int,
    "terminal_font_size": int,
    "terminal_cursor_blink": bool,
    "terminal_scrollback": int,
    "terminal_line_height": float,
    "terminal_cursor_width": int,
    "terminal_letter_spacing": int,
    "terminal_min_contrast": float,
    "terminal_convert_eol": bool,
    "terminal_bold_bright": bool,
    "terminal_copy_on_select": bool,
    "editor_font_size": int,
    "editor_word_wrap": bool,
    "editor_minimap": bool,
    "editor_line_numbers": bool,
    "editor_tab_size": int,
    "editor_detect_indentation": bool,
    "editor_cursor_blink": bool,
    "editor_font_ligatures": bool,
    "editor_sticky_scroll": bool,
    "editor_auto_save": bool,
    "editor_use_spaces": bool,
    "editor_folding": bool,
    "editor_glyph_margin": bool,
    "editor_format_on_paste": bool,
    "editor_rulers": bool,
    "editor_word_wrap_column": int,
    "review_split_view": bool,
    "review_collapse_unified": bool,
    "review_wrap": bool,
    "review_word_diff": bool,
    "review_hide_whitespace": bool,
    "review_full_file": bool,
    "review_max_lines": int,
    "browser_record_history": bool,
    "browser_do_not_track": bool,
    "browser_deny_site_permissions": bool,
    "browser_block_popups": bool,
    "browser_zoom_percent": int,
    "browser_ask_before_download": bool,
    "browser_block_third_party_cookies": bool,
    "browser_fresh_session": bool,
    "browser_force_light_page": bool,
    "browser_history_limit": int,
    "device_context_enabled": bool,
    "network_proxy_port": int,
}

_GROUP_FIELDS: dict[str, frozenset[str]] = {
    "models": _MODEL_FIELDS,
    "agents": _AGENT_FIELDS,
    "ui": UI_SETTING_KEYS,
    "adapters": frozenset({"adapter_configs"}),
    "mcp": frozenset({"mcp_servers"}),
}


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def config_dir(ensure: bool = True) -> Path:
    """Return the directory holding the split configuration files."""
    return get_data_dir(CONFIG_DIR_NAME, ensure=ensure)


def group_path(group: str, ensure: bool = True) -> Path:
    """Return the file backing *group* (see :data:`GROUP_FILES`)."""
    try:
        name = GROUP_FILES[group]
    except KeyError:
        raise ValueError(f"unknown config group: {group!r}") from None
    if ensure:
        config_dir(ensure=True)
    return config_dir(ensure=False) / name


def legacy_config_path() -> Path:
    """Return the pre-split ``model/config.toml`` path (read-only use)."""
    return get_data_dir(*_LEGACY_CONFIG_PARTS, ensure=False)


def legacy_settings_path() -> Path:
    """Return the pre-split top-level ``settings.json`` path."""
    return get_data_dir(_LEGACY_SETTINGS_NAME, ensure=False)


def is_legacy_config_path(path: str | os.PathLike[str] | None) -> bool:
    """True when *path* refers to the pre-split config file.

    Used by :meth:`EncreConfig.save` to tell "persist the whole config"
    (no path, or the legacy canonical path -- what ``_persist_config``
    passes) apart from "export to this exact file" (a custom ``--config``
    path).  Both must keep working.
    """
    if path is None:
        return False
    try:
        return Path(path).expanduser().resolve() == legacy_config_path().resolve()
    except OSError:
        return False


# --------------------------------------------------------------------------
# Low-level encrypted JSON I/O
# --------------------------------------------------------------------------


def _read_encrypted_json(path: Path) -> dict[str, Any] | None:
    """Read and decode an encrypted JSON file, or ``None`` if unusable.

    Tolerates a plaintext JSON file as well, so a file written by an older
    build (or hand-edited) still loads instead of silently resetting the
    user's configuration to defaults.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw.strip():
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    candidates: list[bytes] = []
    try:
        candidates.append(decrypt(text).encode("utf-8"))
    except Exception:
        logger.debug("exception suppressed", exc_info=True)
    candidates.append(raw)  # plaintext fallback
    for blob in candidates:
        try:
            data = json.loads(blob.decode("utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def _write_encrypted_json(path: Path, data: dict[str, Any]) -> None:
    """Encrypt *data* and replace *path* atomically.

    Writes to a sibling temp file then ``os.replace``s it, so a crash (or a
    concurrent reader) never observes a half-written config -- the previous
    implementation used a bare ``write_text``, despite documenting atomicity.
    """
    raw = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
    payload = encrypt(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _coerce_ui(key: str, value: Any) -> Any:
    """Coerce a UI setting to its declared type when one is known."""
    want = _UI_KEY_TYPES.get(key)
    if want is None or isinstance(value, want):
        return value
    if want is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    try:
        return want(value)
    except (TypeError, ValueError):
        return value


# --------------------------------------------------------------------------
# Group routing
# --------------------------------------------------------------------------


def group_of(key: str) -> str:
    """Return the group that owns *key*.

    Adapter fields are stored flattened as ``adapter_<id>_<field>`` (the
    shape the frontend protocol uses), so they are matched by prefix.
    Anything unclaimed falls through to ``runtime`` -- new
    :class:`~encre.config.EncreConfig` fields therefore persist without
    needing this table to be updated.
    """
    if key.startswith("adapter_"):
        return "adapters"
    for group, fields in _GROUP_FIELDS.items():
        if key in fields:
            return group
    return "runtime"


# --------------------------------------------------------------------------
# Read / write
# --------------------------------------------------------------------------


def _read_groups() -> dict[str, dict[str, Any]]:
    """Read every group file, omitting the empty ones."""
    out: dict[str, dict[str, Any]] = {}
    for group in GROUP_FILES:
        data = _read_encrypted_json(group_path(group, ensure=False))
        if data:
            data.pop(_SCHEMA_KEY, None)
            out[group] = data
    return out


def load_settings_file() -> dict[str, Any]:
    """Return the flat UI/overlay store (``config/ui.json``).

    Raw accessor -- it sees only what that one file holds.  Consumers that
    need the flat view the pre-split ``settings.json`` used to provide
    (gateway, authz, relay) must use :func:`overlay_view` instead, which
    also borrows the credentials that live in ``config/adapters.json``.
    """
    data = _read_encrypted_json(group_path("ui", ensure=False))
    if not data:
        return {}
    data.pop(_SCHEMA_KEY, None)
    return {k: _coerce_ui(k, v) for k, v in data.items()}


def save_settings_file(settings: dict[str, Any]) -> None:
    """Merge *settings* into the flat UI/overlay store.

    Merging rather than replacing matches the established semantics: callers
    (the settings panel, ``save_custom_slash_commands``) each own a *subset*
    of the keys, and a wholesale replace would silently drop the rest -- the
    original write path did an explicit read-modify-write for this reason.
    """
    current = _read_encrypted_json(group_path("ui", ensure=False)) or {}
    current.pop(_SCHEMA_KEY, None)
    current.update({k: _coerce_ui(k, v) for k, v in settings.items()})
    current[_SCHEMA_KEY] = _SCHEMA_VERSION
    _write_encrypted_json(group_path("ui"), current)


def overlay_view() -> dict[str, Any]:
    """Flat key/value view for the consumers of the old ``settings.json``.

    Composed of, in this order:

    1. the UI/overlay file itself (including non-schema keys such as
       ``custom_slash_commands`` and the ``gateway_*`` flags);
    2. flattened adapter credentials from ``config/adapters.json`` -- the
       gateway reads ``adapter_<platform>_<field>`` from this view;
    3. ``permission_settings`` from ``config/runtime.json``.

    Items 2 and 3 are *borrowed*: they have exactly one home (their group
    file) and are surfaced here only so existing callers keep working.
    """
    view = load_settings_file()
    view.update(adapter_flat_view())
    runtime = _read_encrypted_json(group_path("runtime", ensure=False)) or {}
    if runtime.get("permission_settings"):
        view["permission_settings"] = runtime["permission_settings"]
    return view


def _overlay_owned(key: str) -> bool:
    """True when the flat overlay is allowed to *store* (not just surface) *key*.

    ``adapter_*`` and ``permission_settings`` belong to their own group files;
    the legacy overlay held copies of them purely because the old write path
    mirrored everything, and copying those copies forward is exactly how the
    duplication would survive the migration.
    """
    return not key.startswith(_ADAPTER_PREFIX) and key not in _BORROWED_KEYS


def save_overlay(settings: dict[str, Any]) -> None:
    """Persist the flat overlay, ignoring borrowed keys.

    ``adapter_*`` and ``permission_settings`` are owned by
    ``config/adapters.json`` / ``config/runtime.json`` and are written from
    the typed model, so a read-modify-write through :func:`overlay_view`
    must never copy them back into the overlay -- that is precisely how the
    old code ended up storing every adapter credential twice.
    """
    save_settings_file({k: v for k, v in settings.items() if _overlay_owned(k)})


def adapter_flat_view() -> dict[str, Any]:
    """Return the flattened ``adapter_<id>_<field>`` credentials."""
    data = _read_encrypted_json(group_path("adapters", ensure=False)) or {}
    data.pop(_SCHEMA_KEY, None)
    return {k: v for k, v in data.items() if k.startswith(_ADAPTER_PREFIX)}


def load_config() -> dict[str, Any]:
    """Load the whole configuration as one flat dict.

    This is the split-layout equivalent of reading the old monolithic
    ``model/config.toml``: every group is merged back into a single dict,
    with the UI/overlay layer applied last so its precedence over stale
    config values is preserved.
    """
    migrate_legacy()
    merged: dict[str, Any] = {}
    for group, data in _read_groups().items():
        if group == "ui":
            continue  # applied last, it wins
        merged.update(data)
    ui = load_settings_file()
    merged.update({k: v for k, v in ui.items() if k in UI_SETTING_KEYS})
    return merged


def save_config(data: dict[str, Any]) -> None:
    """Split *data* by responsibility and write every group file.

    Keys that are ``None`` are dropped (matching
    :meth:`EncreConfig.save`).  The UI group preserves any non-schema keys
    already on disk (e.g. ``custom_slash_commands``, which is not an
    :class:`~encre.config.EncreConfig` field) instead of clobbering them.
    """
    buckets: dict[str, dict[str, Any]] = {group: {} for group in GROUP_FILES}
    for key, value in data.items():
        if value is None:
            continue
        buckets[group_of(key)][key] = value

    # The UI bucket is a flat overlay shared with the settings panel --
    # merge over what is already there so foreign keys survive.
    existing_ui = _read_encrypted_json(group_path("ui", ensure=False)) or {}
    existing_ui.pop(_SCHEMA_KEY, None)
    existing_ui.update({k: _coerce_ui(k, v) for k, v in buckets["ui"].items()})
    buckets["ui"] = existing_ui

    for group, bucket in buckets.items():
        # Every group file is (re)written, even when empty: that is what makes
        # removals stick.  Unbinding the last adapter has to clear
        # adapters.json, otherwise the stale credentials would be re-read on
        # the next start (the old code needed a bespoke "stale keys" sweep
        # for exactly this reason).
        bucket = dict(bucket)
        bucket[_SCHEMA_KEY] = _SCHEMA_VERSION
        _write_encrypted_json(group_path(group), bucket)


def save_group(group: str, data: dict[str, Any]) -> None:
    """Write a single group, replacing its file contents."""
    payload = dict(data)
    payload[_SCHEMA_KEY] = _SCHEMA_VERSION
    _write_encrypted_json(group_path(group), payload)


# --------------------------------------------------------------------------
# Legacy migration
# --------------------------------------------------------------------------


def _rename_aside(path: Path, stamp: str) -> str | None:
    """Rename *path* to ``<name>.migrated-<stamp>``; return the new name."""
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.migrated-{stamp}")
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target.name


def migrate_legacy(force: bool = False) -> dict[str, Any]:
    """Split the pre-split config files into the new layout.

    Runs at most once: after a successful migration the legacy files are
    renamed aside, so a second call is a no-op.  Returns a report dict with
    ``migrated`` (bool), ``groups`` written and ``renamed`` files.

    Args:
        force: Re-run even if the new layout already exists (a later legacy
            value would then overwrite the current one -- only for repair).
    """
    report: dict[str, Any] = {"migrated": False, "groups": [], "renamed": []}

    legacy_cfg = legacy_config_path()
    legacy_set = legacy_settings_path()
    already_split = any(
        group_path(g, ensure=False).exists() for g in GROUP_FILES
    )
    if already_split and not force:
        return report

    blob = _read_encrypted_json(legacy_cfg)
    overlay = _read_encrypted_json(legacy_set)
    if not blob and not overlay:
        return report

    merged: dict[str, Any] = dict(blob or {})
    extras: dict[str, Any] = {}
    # The overlay wins over the blob, exactly as it did at runtime.
    for key, value in (overlay or {}).items():
        if key in UI_SETTING_KEYS:
            merged[key] = _coerce_ui(key, value)
        elif key == "permission_settings":
            merged[key] = value
        elif _overlay_owned(key):
            # Keys with no dataclass field (custom_slash_commands, gateway_*)
            # are not part of the typed model, so they must be carried over
            # by hand instead of being dropped by save_config.
            extras[key] = value

    save_config(merged)
    if extras:
        ui = _read_encrypted_json(group_path("ui", ensure=False)) or {}
        ui.pop(_SCHEMA_KEY, None)
        for key, value in extras.items():
            ui.setdefault(key, value)
        save_group("ui", ui)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    renamed = [n for n in (_rename_aside(legacy_cfg, stamp), _rename_aside(legacy_set, stamp)) if n]

    report.update({
        "migrated": True,
        "groups": [g for g in GROUP_FILES if group_path(g, ensure=False).exists()],
        "renamed": renamed,
        "keys": len(merged) + len(extras),
        "extras": sorted(extras),
    })
    return report
