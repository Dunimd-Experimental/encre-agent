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

"""Workspace persistence: registry, per-workspace config, icons, indices.

Module-level helpers backing the iwork workspace system used by
:meth:`encre.protocol.handlers.workspaces.WorkspacesHandlers` and the
session manager.  Everything here is plain filesystem state under
``<data_dir>/iwork`` plus a ``.encre`` directory inside each project
folder; there is no database.

Four storage areas are managed:

* **Workspace registry** -- ``<data_dir>/iwork/index.json``, an encrypted
  JSON list of ``{id, path, name, opened_at, created_at}`` records.  The
  id is a stable 12-char sha256 prefix of the folder path, so moving a
  workspace back to the same path resumes the same identity.
* **Per-workspace config** -- ``<workspace>/.encre/config.json``, a
  plaintext JSON overlay applied on top of the global config when the
  workspace is opened.
* **Icons** -- ``<data_dir>/iwork/<id>/icon.svg`` (generated letter
  avatar) or ``icon.png`` (custom upload); only one is ever considered,
  with uploads taking precedence.
* **Session index** -- the workspace-scoped session index at
  ``sessions/index.json``.

Public API:
  - :func:`_load_workspaces` / :func:`_save_workspaces` -- registry I/O.
  - :func:`_apply_workspace_config` -- overlay a workspace config onto a
    live ``AgentConfig``.
  - :func:`_workspaces_with_session_counts` -- frontend-facing view with
    session counts and icon data URLs attached.
  - :func:`_remove_session_from_workspace_indices` -- drop a session from
    every workspace index.
"""

import base64
import contextlib
import json
import logging
import os
import time
from typing import Any

from encre.config import get_data_dir

logger = logging.getLogger("encre.transport.ws")


# ------------------------------------------------------------------
# Workspace persistence
# ------------------------------------------------------------------

def _workspace_config_path(workspace_path: str) -> str:
    """Path to the per-workspace config file.

    Args:
        workspace_path: Absolute path of the project folder.

    Returns:
        ``<workspace_path>/.encre/config.json``.
    """
    return os.path.join(workspace_path, ".encre", "config.json")


def _workspace_context_files(workspace_path: str) -> dict[str, bool]:
    """Report which workspace context files exist.

    The agent prompt is assembled from whichever of ``AGENTS.md`` and
    ``CLAUDE.md`` are present, so the settings panel needs to know ahead
    of time which ones it can toggle.

    Args:
        workspace_path: Absolute path of the project folder; an empty or
            non-existent path reports both as absent rather than raising.

    Returns:
        A mapping of filename to whether it exists.
    """
    if not workspace_path or not os.path.isdir(workspace_path):
        return {"AGENTS.md": False, "CLAUDE.md": False}
    return {
        name: os.path.isfile(os.path.join(workspace_path, name))
        for name in ("AGENTS.md", "CLAUDE.md")
    }


def _load_workspace_config(workspace_path: str) -> dict[str, Any]:
    """Read the per-workspace config document.

    Every failure mode degrades to an empty dict: a missing file, an
    empty file and a malformed or non-object JSON payload are all
    indistinguishable to the caller, because an absent workspace config
    simply means "inherit everything from the global config".

    Args:
        workspace_path: Absolute path of the project folder.

    Returns:
        The parsed config dict, or ``{}`` when absent, empty or invalid.
    """
    ws_config_path = _workspace_config_path(workspace_path)
    if not os.path.isfile(ws_config_path):
        return {}
    try:
        with open(ws_config_path, encoding="utf-8") as f:
            raw = f.read().strip()
            if not raw:
                return {}
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
    except Exception:
        logger.warning("Failed to load workspace config from %s", ws_config_path, exc_info=True)
        return {}


def _save_workspace_config(workspace_path: str, partial: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial per-workspace config into ``.encre/config.json``.

    The write is an overlay rather than a replacement: unknown keys are
    dropped so a stray field from a newer client cannot poison the file,
    and a ``None`` value deletes the key, which is how the caller clears
    an override and returns to inheriting from the global config.

    Args:
        workspace_path: Absolute path of the project folder.
        partial: The keys to add or replace.

    Returns:
        The merged config dict that was actually written, so the caller
        can echo the canonicalised view back to the client.
    """
    known_keys = {
        "system_prompt", "permission_mode", "specialty", "max_turns",
        "language", "models", "models_enabled", "active_model", "permissions",
        "mcp", "context_files",
    }
    os.makedirs(os.path.join(workspace_path, ".encre"), exist_ok=True)
    current = _load_workspace_config(workspace_path)
    for key, value in (partial or {}).items():
        if key not in known_keys:
            continue
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    try:
        with open(_workspace_config_path(workspace_path), "w", encoding="utf-8") as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.warning("Failed to save workspace config to %s", _workspace_config_path(workspace_path), exc_info=True)
    return current


def _apply_workspace_config(config: Any, workspace_path: str) -> None:
    """Overlay the workspace config onto a live agent config.

    Called when a workspace is opened, before its first session is
    created, so every session under that project inherits the same
    overrides.  Only fields present in the file are touched; anything
    omitted keeps the global value.

    The model pool is the most stateful part: ``models_enabled`` is the
    master switch, so an explicit ``false`` disables selection outright
    (the stored ids are kept so the user can re-enable later), an enabled
    pool with ids restricts the session to those ids, and an enabled pool
    with no ids is an empty pool that must refuse runs entirely.  When
    ids are present the active model is pinned onto the first eligible
    pool member so the very first request does not accidentally use a
    global active model that lives outside the pool.

    ``active_model`` is the workspace's own last selection, stored as a
    model identity rather than an index.  It wins over the pool's first
    member when it is still enabled and inside the pool, which is what
    makes every conversation in a project share one model across restarts.

    ``context_files`` is an allowlist in inverted form: a file listed as
    disabled never reaches the model, which is how a project opts out of
    one of its two prompt sources without deleting it.

    Args:
        config: The mutable agent config to patch in place.
        workspace_path: Absolute path of the project folder.

    Raises:
        Nothing; a malformed file is logged and the config is left
        untouched.
    """
    ws_config = _load_workspace_config(workspace_path)
    if not ws_config:
        return
    try:
        if "system_prompt" in ws_config:
            config.system_prompt = ws_config["system_prompt"]
        if "permission_mode" in ws_config:
            config.permission_mode = ws_config["permission_mode"]
        if "specialty" in ws_config:
            config.default_specialty = ws_config["specialty"]
        if "max_turns" in ws_config:
            config.max_turns = ws_config["max_turns"]
        if "language" in ws_config:
            config.language = ws_config["language"]
        # Restrict which models may serve this workspace's sessions.  The
        # models_enabled flag is the master switch: when explicitly false,
        # any stored model ids are ignored (selection is off, though the ids
        # may still be kept in the file so the user can re-enable later).
        # When enabled but no model ids are configured, the pool is empty and
        # runs must be refused (workspace_pool_state == "empty").
        pool_flag = ws_config.get("models_enabled")
        raw_models = ws_config.get("models")
        pool_ids: list[str] = []
        if pool_flag is False:
            config.target_model_ids = []
            config.workspace_pool_state = "off"
        elif pool_flag is True or "models" in ws_config:
            from encre.model_selection import parse_target_model_ids
            ids = parse_target_model_ids(raw_models) or []
            pool_ids = list(ids)
            config.target_model_ids = list(ids)
            if ids:
                config.workspace_pool_state = "on"
                # Pin the active model onto the first eligible pool member so
                # the very first request uses a pool model, not the global
                # active model that may live outside the pool.
                for _i, _m in enumerate(config.models):
                    if _m.model_id in ids and getattr(_m, "enabled", True):
                        config.active_model_index = _i
                        config.apply_active_model()
                        break
            else:
                # Pool enabled but nothing selected -> no usable model at all.
                config.workspace_pool_state = "empty"
        else:
            config.workspace_pool_state = "off"
        # The workspace remembers which model its conversations last used.
        # Applied after the pool pin so the explicit choice wins, but only
        # when that model is still enabled and inside the pool -- otherwise
        # the pool pin (or the inherited default) stays in charge.
        from encre.model_selection import resolve_model_index
        active_index = resolve_model_index(config, ws_config.get("active_model"))
        if active_index is not None:
            active_model = config.models[active_index]
            in_pool = not pool_ids or active_model.model_id in pool_ids
            if in_pool and getattr(active_model, "enabled", True):
                config.active_model_index = active_index
                config.apply_active_model()
        # Per-file toggles for workspace context files (AGENTS.md / CLAUDE.md).
        # A file listed here as disabled is excluded, so its contents never
        # reach the model.
        if "context_files" in ws_config:
            cf = ws_config["context_files"]
            if isinstance(cf, dict):
                config.disabled_context_files = [
                    str(name) for name, enabled in cf.items() if not enabled
                ]
            else:
                config.disabled_context_files = []

        logger.info("Applied workspace config from %s", _workspace_config_path(workspace_path))
    except Exception:
        logger.warning("Failed to load workspace config", exc_info=True)


def _get_workspaces_path() -> str:
    """Path of the encrypted workspace registry file.

    ``ensure=False`` is mandatory here: the path is a FILE, not a
    directory.  The default ``ensure=True`` calls ``mkdir()`` on it, which
    raises ``FileExistsError`` (WinError 183) as soon as the file exists --
    and since this call happens before the loader's own try block, the
    exception escaped and silently killed the WebSocket connection on every
    ``open_workspace``.

    Returns:
        The registry path as a string.  A stale directory left behind by
        the old bug is removed before returning so the next save can write
        through it.
    """
    path = get_data_dir("iwork", "index.json", ensure=False)
    if path.is_dir():
        # Self-heal a directory left behind by the old mkdir bug.
        with contextlib.suppress(OSError):
            path.rmdir()
    return str(path)


def _make_workspace_id(folder_path: str) -> str:
    """Generate a stable workspace id from the folder path.

    A sha256 prefix of the normalised path means the same folder always
    maps to the same identity across restarts and machines, which is what
    makes sessions, index caches and icons survive a workspace close.

    Args:
        folder_path: Absolute path of the project folder.

    Returns:
        The first 12 hex characters of the sha256 digest.
    """
    import hashlib
    return hashlib.sha256(folder_path.encode()).hexdigest()[:12]


def _get_workspace_dir(ws_id: str) -> str:
    """Data directory for a workspace.

    Args:
        ws_id: Workspace id from :func:`_make_workspace_id`.

    Returns:
        ``<data_dir>/iwork/<ws_id>`` as a string.
    """
    return str(get_data_dir("iwork", ws_id))


def _find_workspace_path(ws_id: str) -> str | None:
    """Return the absolute filesystem path for a workspace id.

    Args:
        ws_id: Workspace id to look up.

    Returns:
        The folder path, or ``None`` when the workspace is not registered.
    """
    for ws in _load_workspaces():
        if ws.get("id") == ws_id:
            return ws.get("path", "")
    return None


def _remove_session_from_workspace_indices(session_id: str) -> None:
    """Remove a session id from every workspace session index.

    Workspace ``sessions/index.json`` files are separate from the main
    session manager's index and are loaded independently by
    ``list_sessions``, so a deleted session would reappear on page refresh
    unless it is removed from each one here.

    The session's on-disk folder is purged in the same pass, routed through
    the unified :func:`encre.lifecycle.purge_session` entry point so its
    telemetry, rollback points, sub-agent stores and spillover files are
    all cleaned together.  Clearing the index entry on its own is exactly
    what left empty ``<session_id>/`` directories behind.

    Args:
        session_id: Session id to drop from every workspace index.
    """
    for ws in _load_workspaces():
        ws_id = ws.get("id") or _make_workspace_id(ws["path"])
        ws_dir = _get_workspace_dir(ws_id)
        idx_file = os.path.join(ws_dir, "sessions", "index.json")
        if not os.path.isfile(idx_file):
            continue
        try:
            with open(idx_file, encoding="utf-8") as f:
                raw = f.read().strip()
            if raw and not raw.startswith("{"):
                from encre.crypto import decrypt
                with contextlib.suppress(Exception):
                    raw = decrypt(raw)
            idx = json.loads(raw)
            if not isinstance(idx, dict):
                continue
            if session_id in idx:
                del idx[session_id]
                new_raw = json.dumps(idx, ensure_ascii=False, separators=(",", ":"))
                try:
                    from encre.crypto import encrypt
                    new_raw = encrypt(new_raw)
                except Exception:
                    pass
                with open(idx_file, "w", encoding="utf-8") as f:
                    f.write(new_raw)
                # The index entry is gone, so take the session folder with
                # it.  Clearing the index alone is what left empty
                # ``<session_id>/`` directories behind.  Routed through the
                # unified lifecycle entry point so the session's telemetry,
                # rollback points, sub-agent stores and spillover files are
                # cleaned in the same pass.
                from encre.lifecycle import purge_session

                purge_session(session_id, sessions_dir=os.path.join(ws_dir, "sessions"))
        except Exception:
            continue


def _ensure_workspace_dirs(ws_id: str) -> str:
    """Create the workspace data directories.

    Args:
        ws_id: Workspace id.

    Returns:
        The workspace data directory path, with its ``sessions``
        subdirectory created.
    """
    ws_dir = _get_workspace_dir(ws_id)
    os.makedirs(os.path.join(ws_dir, "sessions"), exist_ok=True)
    return ws_dir


def _load_workspaces() -> list[dict[str, Any]]:
    """Load the encrypted workspace registry, migrating legacy records.

    Three migrations run in place on every read and are saved back only if
    something actually changed:

    * records without an ``id`` get one derived from the path;
    * records without ``created_at`` get it backfilled from the oldest
      timestamp we already have (``opened_at``), never recomputed from the
      current time;
    * records with an embedded ``icon_data`` blob have it flushed to the
      on-disk icon file and dropped, since the file is the source of truth.

    Entries with an empty path or name are filtered out, so a half-written
    record cannot reach the frontend.

    Returns:
        The list of workspace records; an empty list when the file is
        absent, empty, unreadable or fails to decrypt.
    """
    path = _get_workspaces_path()
    if not os.path.exists(path):
        return []
    try:
        from encre.crypto import decrypt
        with open(path, encoding="utf-8") as f:
            encrypted = f.read()
        if not encrypted.strip():
            return []
        raw = decrypt(encrypted)
        workspaces: list[dict[str, Any]] = json.loads(raw)
        # Migrate records that predate these fields, in place.
        migrated = False
        for w in workspaces:
            if "id" not in w:
                w["id"] = _make_workspace_id(w["path"])
                migrated = True
            if "created_at" not in w:
                # Backfill from the oldest timestamp on record; never
                # recompute, so creation time stays honest across upgrades.
                w["created_at"] = float(w.get("opened_at") or 0) or time.time()
                migrated = True
            if "icon_data" in w:
                # The on-disk icon file is the source of truth, so any
                # embedded copy is flushed out and dropped from the record.
                try:
                    ws_id = w["id"]
                    payload = w["icon_data"]
                    ext = "svg" if payload.startswith("data:image/svg") else "png"
                    icon_path = _get_workspace_icon_file(ws_id, ext)
                    if ";base64," in payload and not os.path.exists(icon_path):
                        os.makedirs(os.path.dirname(icon_path), exist_ok=True)
                        with open(icon_path, "wb") as fh:
                            fh.write(base64.b64decode(payload.split(";base64,", 1)[1]))
                except Exception:
                    logger.warning("legacy workspace icon flush failed", exc_info=True)
                finally:
                    w.pop("icon_data", None)
                migrated = True
        if migrated:
            _save_workspaces(workspaces)
        # Filter out invalid entries (empty path or name)
        workspaces = [w for w in workspaces if w.get("path") and w.get("name")]
        return workspaces
    except Exception:
        logger.warning("Failed to load workspaces", exc_info=True)
        return []


def _save_workspaces(workspaces: list[dict[str, Any]]) -> None:
    """Persist the workspace registry as an encrypted JSON document.

    The whole list is rewritten on every call (no append), which keeps the
    file consistent after the caller has capped, migrated or removed
    entries.  A write failure is logged and swallowed so a read-only
    filesystem cannot take down the connection.

    Args:
        workspaces: The full list of workspace records to store.
    """
    path = _get_workspaces_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        from encre.crypto import encrypt
        raw = json.dumps(workspaces, ensure_ascii=False)
        encrypted = encrypt(raw)
        with open(path, "w", encoding="utf-8") as f:
            f.write(encrypted)
    except Exception:
        logger.warning("Failed to save workspaces", exc_info=True)


# ------------------------------------------------------------------
# Workspace icons
# ------------------------------------------------------------------
# Each workspace's icon lives as a real file inside its data folder:
# ``icon.svg`` (generated default) or ``icon.png`` (a custom upload). Outgoing
# workspace payloads get the icon attached as a ``data:`` URL by
# ``_workspaces_with_session_counts`` so the frontend never touches the
# filesystem. The default icon is generated as SVG -- a hashed random
# background colour plus the capitalised first character of the workspace name.
_ICON_SIZE = 128


def _get_workspace_icon_file(ws_id: str, ext: str) -> str:
    """Path of the workspace's on-disk icon file.

    Args:
        ws_id: Workspace id.
        ext: ``"svg"`` for the generated default or ``"png"`` for a custom
            upload.

    Returns:
        ``<ws_dir>/icon.<ext>``.
    """
    return os.path.join(_get_workspace_dir(ws_id), f"icon.{ext}")


def _write_workspace_icon_file(ws_id: str, ext: str, data: bytes) -> None:
    """Persist icon bytes to ``<ws_dir>/icon.<ext>`` with an atomic replace.

    Writes to a ``.tmp`` sibling first and then ``os.replace``s it into
    place, so a crash mid-write cannot leave a half-written icon that the
    frontend would render as broken.

    Args:
        ws_id: Workspace id.
        ext: File extension, ``"svg"`` or ``"png"``.
        data: Raw icon bytes.
    """
    path = _get_workspace_icon_file(ws_id, ext)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _remove_workspace_icon_file(ws_id: str, ext: str) -> None:
    """Delete a workspace icon file, ignoring a missing file.

    Used when a custom PNG upload supersedes the generated SVG default.

    Args:
        ws_id: Workspace id.
        ext: File extension to remove.
    """
    with contextlib.suppress(OSError):
        os.remove(_get_workspace_icon_file(ws_id, ext))


def _read_workspace_icon_data_url(ws_id: str) -> str | None:
    """Load the workspace icon and return it as a ``data:`` URL.

    Custom uploads (``icon.png``) take precedence over the generated
    default (``icon.svg``), so uploads are checked first.

    Args:
        ws_id: Workspace id.

    Returns:
        A base64 ``data:`` URL, or ``None`` when no icon file exists.
    """
    for ext, mime in (("png", "image/png"), ("svg", "image/svg+xml")):
        try:
            with open(_get_workspace_icon_file(ws_id, ext), "rb") as f:
                return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")
        except OSError:
            continue
    return None


_ICON_FONT_STACK = "'Microsoft YaHei','PingFang SC','Noto Sans SC',Arial,sans-serif"


def _generate_default_workspace_icon(name: str) -> bytes:
    """Render the fallback icon for a workspace name as SVG bytes.

    The name is hashed into an HSL hue so every workspace gets a distinct
    colour that is arbitrary-looking but stable across restarts -- a
    re-rolled colour on every launch would read as a bug.  The glyph is
    the capitalised first character of the name, escaped so a name
    containing XML metacharacters cannot break the document.

    Args:
        name: Workspace display name.

    Returns:
        UTF-8 encoded SVG bytes, 128x128 with rounded corners.
    """
    import colorsys
    from xml.sax.saxutils import escape as _escape

    # Hash the name into a hue so the colour is arbitrary-looking but stable
    # across restarts (a re-rolled colour on every launch would read as a bug).
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    r, g, b = (int(c * 255) for c in colorsys.hls_to_rgb((h % 360) / 360.0, 0.46, 0.62))
    ch = _escape(name.strip()[:1] or "?").upper()

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_ICON_SIZE}" height="{_ICON_SIZE}" '
        f'viewBox="0 0 {_ICON_SIZE} {_ICON_SIZE}">'
        f'<rect width="{_ICON_SIZE}" height="{_ICON_SIZE}" rx="20" fill="rgb({r},{g},{b})"/>'
        f'<text x="{_ICON_SIZE // 2}" y="{_ICON_SIZE // 2}" text-anchor="middle" '
        f'dominant-baseline="central" font-family="{_ICON_FONT_STACK}" '
        f'font-size="52" font-weight="700" fill="#ffffff">{ch}</text>'
        "</svg>"
    )
    return svg.encode("utf-8")


def _normalize_workspace_icon(raw: bytes) -> bytes | None:
    """Decode uploaded image bytes into square 128px PNG bytes.

    Centre-crops non-square images, composites alpha over white so the
    icon does not render as transparent, and returns ``None`` when the
    bytes cannot be decoded as an image at all.

    Args:
        raw: Raw uploaded image bytes.

    Returns:
        PNG bytes, or ``None`` on decode failure.
    """
    from io import BytesIO

    from PIL import Image

    try:
        img = Image.open(BytesIO(raw)).convert("RGBA")
        side = min(img.size)
        left = (img.width - side) // 2
        top = (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side)).resize((_ICON_SIZE, _ICON_SIZE))
        flat = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (255, 255, 255, 255))
        flat.alpha_composite(img)
        buf = BytesIO()
        flat.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _workspaces_with_session_counts(workspaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return workspace records enriched with live counts and icons.

    Each record gets a ``session_count`` derived from the workspace's own
    session index, excluding automation and sub-agent entries so the number
    matches what the sidebar actually lists.  The icon is loaded from disk
    and attached as a ``data:`` URL, so the frontend never touches the
    filesystem.

    Enriched values are view-only: they are never persisted back to the
    registry, so the stored records stay free of derived state.

    Args:
        workspaces: Registry records to enrich.

    Returns:
        New records with the derived fields added.
    """
    result: list[dict[str, Any]] = []
    for ws in workspaces:
        entry = dict(ws)
        ws_id = entry.get("id") or _make_workspace_id(entry["path"])
        # Icons live on disk; ignore any legacy in-record copies here (they
        # are flushed to files by the _load_workspaces migration).
        entry.pop("icon_data", None)
        icon_data_url = _read_workspace_icon_data_url(ws_id)
        if icon_data_url:
            entry["icon_data"] = icon_data_url
        idx_file = os.path.join(_get_workspace_dir(ws_id), "sessions", "index.json")
        count = 0
        if os.path.isfile(idx_file):
            try:
                with open(idx_file, encoding="utf-8") as f:
                    raw = f.read().strip()
                if raw and not raw.startswith("{"):
                    from encre.crypto import decrypt as _decrypt
                    with contextlib.suppress(Exception):
                        raw = _decrypt(raw)
                idx = json.loads(raw)
                if isinstance(idx, dict):
                    count = sum(
                        1
                        for e in idx.values()
                        if isinstance(e, dict) and e.get("channel") not in ("automation", "sub_agent")
                    )
            except Exception:
                count = 0
        entry["session_count"] = count
        result.append(entry)
    return result
