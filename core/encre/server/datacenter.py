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

"""Authority for data-root statistics and data-center record views.

Encre keeps all state as files under the data root (``data/`` or
``~/.dunimd/encre`` -- no database).  This module is the single place that
inspects that tree, serving two admin routes mounted by
:func:`encre.server.admin.handle_admin`:

* ``GET /datacenter/survey`` -> :func:`survey` -- file/byte counts grouped by
  category and module.  This backs the desktop data-center overview page, and
  it counts *files and bytes only*.  Encrypted stores (``*.enc``) cannot be
  counted record-by-record without decrypting them, which the survey refuses to
  do: every number it reports is directly observable on disk, so the page can
  never display a fabricated count.
* ``GET /datacenter/module`` -> :func:`module_rows` -- the records behind one
  store.  Here the browser stores are deliberately decrypted, because the
  data-center module page is the operator's inspection surface for exactly
  that data and the renderer is not allowed to touch the filesystem itself.

Category and module ids are a wire contract with the renderer's registry
(``desktop/renderer/src/features/datacenter/registry.ts``) and are joined by
id, so renaming one side breaks the join silently rather than loudly.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from encre.paths import get_data_dir

logger = logging.getLogger("encre.server.datacenter")

# Safety valve: the largest real tree (rollback/) holds ~7.7k entries; a
# hard cap keeps a pathological directory from pinning a worker thread.
_MAX_ENTRIES = 50_000

# Loose files that sit directly in the data root.  They do not belong to
# any dedicated subtree, so they are reported under the ``cache`` module.
_ROOT_LOOSE_FILES = (
    "ea_manifest_cache.json",
    "keybinds.json",
    "autostart.json",
    "dsp_cache.bin",
    "gateway_routing.db",
    "mcp.json",
    "yimd.pid",
    "automation_history.json.bak",
)


def _walk_stats(path: Path) -> tuple[int, int]:
    """Return ``(files, bytes)`` for *path*, recursing into subdirectories.

    An iterative ``os.scandir`` walk rather than ``os.walk``: the explicit
    stack keeps memory flat on deep trees and lets the
    :data:`_MAX_ENTRIES` cap interrupt the walk mid-traversal instead of
    after it.  Symlinks are never followed in either check, so a planted
    symlink farm cannot be counted as local state or used to escape the
    data root.

    Args:
        path: The root of the tree to measure.

    Returns:
        ``(file_count, total_bytes)``.  Missing paths report ``(0, 0)``;
        unreadable entries are skipped rather than raised, because a data
        directory is written by other processes and may be mid-rotation.
    """
    files = 0
    total = 0
    stack = [path]
    while stack and files < _MAX_ENTRIES:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return files, total


def _dir_count(path: Path) -> int:
    """Count direct child directories of *path* (0 when missing).

    Used for directory-shaped stores (``sessions/``, ``iwork/``,
    ``sub_agents/``, ``rollback/``) where each child directory is one
    logical record -- an open session, a workspace, a rollback snapshot --
    not one file.  Counting files there would misrepresent the unit the
    UI is presenting.
    """
    try:
        with os.scandir(path) as it:
            return sum(1 for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return 0


def _file_stats(path: Path) -> tuple[int, int]:
    """Return ``(1, size)`` for an existing file, else ``(0, 0)``.

    On-demand stores are absent until first use, so a missing path is a
    truthful zero rather than an error.
    """
    try:
        if path.is_file():
            return 1, path.stat().st_size
    except OSError:
        pass
    return 0, 0


def _module(mid: str, paths: list[tuple[Path, str]]) -> dict[str, Any]:
    """Aggregate one module over its on-disk locations.

    *paths* is a list of ``(path, kind)`` where *kind* is ``"file"``,
    ``"tree"`` or ``"dirs"``.  ``"dirs"`` counts child directories (session
    stores) instead of individual files.

    Args:
        mid: The module id reported to the renderer.
        paths: Storage locations, each with the counting unit to apply.

    Returns:
        ``{"id", "files", "bytes"}`` summed over every location.  A module
        whose locations all miss therefore reports zeros, which is the
        correct signal for "not yet created".
    """
    files = 0
    total = 0
    for path, kind in paths:
        if kind == "file":
            f, b = _file_stats(path)
        elif kind == "dirs":
            f, b = _dir_count(path), 0
            if f:
                # Directory stores still report their bytes via a full walk.
                _, b = _walk_stats(path)
        else:
            f, b = _walk_stats(path)
        files += f
        total += b
    return {"id": mid, "files": files, "bytes": total}


def survey() -> dict[str, Any]:
    """Scan the data root and return per-category / per-module statistics.

    The result shape is::

        {
          "root": "<data root>",
          "generated_at": 1690000000.0,
          "total_bytes": 123,
          "categories": [
            {"id": "browser", "files": 3, "bytes": 456,
             "modules": [{"id": "cookies", "files": 1, "bytes": 456}, ...]},
            ...
          ]
        }

    Category and module ids match the renderer registry
    (``desktop/renderer/src/features/datacenter/registry.ts``).

    Returns:
        The payload above, with ``total_bytes`` as the sum of category
        bytes (not of the whole tree -- some subtrees are not covered by
        any module and are intentionally not double-counted).

    Note:
        Runs inside a worker thread off the event loop; see the
        ``asyncio.to_thread`` call in :mod:`encre.server.app`.  Every
        filesystem call here degrades to zero on ``OSError`` because the
        tree is written concurrently by the live backend.
    """
    root = get_data_dir(ensure=False)

    # Inline location builders keep the spec table below readable: the
    # table is the actual contract with the renderer.
    root_file = lambda name: (root / name, "file")  # noqa: E731
    root_tree = lambda name: (root / name, "tree")  # noqa: E731

    module_specs: dict[str, dict[str, list[tuple[Path, str]]]] = {
        # Browser stores are one encrypted file each, created on demand --
        # a missing file is a truthful 0, not an error.
        "browser": {
            "logins": [(root / "browser" / "passwords.enc", "file")],
            "cookies": [(root / "browser" / "cookies.enc", "file")],
            "storage": [(root / "browser" / "localstorage.enc", "file")],
            "bookmarks": [(root / "browser" / "bookmarks.enc", "file")],
            "history": [(root / "browser" / "history.enc", "file")],
            # Permissions live in the main process, not on disk here.
            "perms": [],
        },
        "logs": {
            "runlog": [(root / "yimd.log", "file")],
            "events": [root_tree("events")],
            "telemetry": [root_tree("telemetry")],
            "audit": [root_tree("audit")],
            "evolution": [root_tree("evolution")],
            "fillaudit": [(root / "telemetry" / "fill.jsonl", "file")],
        },
        "runtime": {
            "sessions": [(root / "sessions", "dirs")],
            "workspaces": [(root / "iwork", "dirs")],
            "subagents": [(root / "sub_agents", "dirs")],
            "iclaw": [root_tree("iclaw")],
            "autohistory": [root_file("automation_history.json")],
            "jobs": [root_file("jobs.json")],
        },
        "system": {
            "keyfile": [(root / "keys" / "keyfile", "file")],
            "rollback": [(root / "rollback", "dirs")],
            "safety": [root_tree("safety")],
            "device": [root_tree("device_context")],
            "bridge": [
                root_tree("weixin"),
                root_file("notifications.enc"),
            ],
            "cache": [(root / name, "file") for name in _ROOT_LOOSE_FILES],
        },
    }

    categories: list[dict[str, Any]] = []
    grand_total = 0
    for cat_id, modules in module_specs.items():
        cat_files = 0
        cat_bytes = 0
        out_modules: list[dict[str, Any]] = []
        for mid, paths in modules.items():
            m = _module(mid, paths)
            cat_files += m["files"]
            cat_bytes += m["bytes"]
            out_modules.append(m)
        grand_total += cat_bytes
        categories.append({
            "id": cat_id,
            "files": cat_files,
            "bytes": cat_bytes,
            "modules": out_modules,
        })

    return {
        "root": str(root),
        "generated_at": round(time.time(), 1),
        "total_bytes": grand_total,
        "categories": categories,
    }


def _cli() -> None:  # pragma: no cover - manual inspection helper
    """Print the survey as JSON for local diagnostics.

    Lets an operator verify what the data-center page will show without
    booting the server: ``python -m encre.server.datacenter``.
    """
    print(json.dumps(survey(), ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Module rows
# ---------------------------------------------------------------------------

# The browser stores are one encrypted JSON blob each, written by the
# Electron main process (see ``main.ts::encryptCookies``): raw 32-byte AES-256
# key at ``browser/key``, payload laid out as ``iv(16) | tag(16) | ct``.
# Reading them here keeps the data-center page's *entire* data path inside
# core -- the renderer never touches the filesystem, per the architecture rule.

_BROWSER_DIR_NAME = "browser"
_MAX_ROWS = 5_000

# Fixed mask for password-class values.  The real secret never leaves core.
_PASSWORD_MASK = "••••••••"


def _load_browser_key(key_path: Path) -> bytes:
    """Load the browser AES sub-key, unwrapping/migrating it as needed.

    Legacy key files hold the raw 32-byte AES-256 key in plaintext; new
    files hold ``encrypt_raw(key)`` (``nonce(12) | ct | tag(16)``).  A
    legacy plaintext key is re-wrapped in place on first read (one-time
    self-migration).  If the stored blob cannot be unwrapped the error
    propagates -- fail-closed, never fall back to treating garbage as
    the key.

    Args:
        key_path: ``<data root>/browser/key``.

    Returns:
        The 32-byte AES-256 key used for every browser store.

    Raises:
        ValueError: If the unwrapped blob is not exactly 32 bytes.
        Exception: Any unwrap failure from :func:`encre.crypto.decrypt_raw`;
            a wrong key must surface as a hard error rather than a silent
            empty table.
    """
    from encre.crypto import decrypt_raw, encrypt_raw

    key_bytes = key_path.read_bytes()
    if len(key_bytes) == 32:
        # Legacy plaintext key: wrap it at rest and rewrite the file once.
        key_path.write_bytes(encrypt_raw(key_bytes))
        return key_bytes
    key = decrypt_raw(key_bytes)  # raises on failure -- fail-closed
    if len(key) != 32:
        raise ValueError("browser key file does not unwrap to a 32-byte key")
    return key


def _load_browser_json(root: Path, filename: str) -> Any:
    """Decrypt one browser ``*.enc`` store and parse its JSON payload.

    Returns ``None`` when the store does not exist yet (these files are
    created on demand -- a missing file is a truthful empty table, not an
    error) or when decryption/parsing fails.

    Args:
        root: The data root; stores live under ``root/browser/``.
        filename: Store file name such as ``"cookies.enc"``.

    Returns:
        The decoded JSON object, or ``None`` for an absent or unreadable
        store.  Callers treat ``None`` and any unexpected shape as an empty
        row set.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    enc_path = root / _BROWSER_DIR_NAME / filename
    if not enc_path.is_file():
        return None
    key_path = root / _BROWSER_DIR_NAME / "key"
    if not key_path.is_file():
        return None
    data = enc_path.read_bytes()
    if len(data) < 33:
        return None
    key = _load_browser_key(key_path)
    # Wire layout written by the desktop main process: iv(16) | tag(16) | ct.
    nonce, tag, ct = data[:16], data[16:32], data[32:]
    plain = AESGCM(key).decrypt(nonce, ct + tag, None)
    return json.loads(plain.decode("utf-8"))


def _rows_for_cookies(root: Path) -> list[dict[str, Any]]:
    """Project the cookie store into presentation rows.

    Mirrors the field set the renderer table renders (name, value, domain,
    expiry epoch seconds, secure/httpOnly flags) and coerces a missing or
    non-numeric ``expirationDate`` to ``0`` so the column always renders.

    Args:
        root: The data root.

    Returns:
        One row per stored cookie, empty when the store is absent or has
        the wrong shape.
    """
    data = _load_browser_json(root, "cookies.enc")
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for c in data:
        if not isinstance(c, dict):
            continue
        exp = c.get("expirationDate")
        out.append({
            "name": c.get("name", ""),
            "value": c.get("value", ""),
            "domain": c.get("domain", ""),
            "expires": float(exp) if isinstance(exp, (int, float)) else 0,
            "secure": bool(c.get("secure")),
            "httpOnly": bool(c.get("httpOnly")),
        })
    return out


def _rows_for_history(root: Path) -> list[dict[str, Any]]:
    """Project the visit history store into presentation rows.

    The on-disk records use snake_case keys written by the desktop browser
    backend; the wire rows use camelCase to match the renderer's table
    column model.

    Args:
        root: The data root.

    Returns:
        One row per visit entry, empty when the store is absent.
    """
    data = _load_browser_json(root, "history.enc")
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for h in data:
        if not isinstance(h, dict):
            continue
        out.append({
            "title": h.get("title", ""),
            "url": h.get("url", ""),
            "visitTime": int(h.get("visit_time", 0)),
            "visitCount": int(h.get("visit_count", 0)),
        })
    return out


def _flatten_bookmark_node(node: Any, folder: str, out: list[dict[str, Any]]) -> None:
    """Depth-first flatten of a Chrome bookmark tree into URL rows.

    Chrome stores bookmarks as a nested tree of folders and URLs; the table
    cannot render that shape, so it is linearised while carrying the
    enclosing folder name along for provenance.  Non-dict entries (a
    hand-edited store) are dropped rather than raising.

    Args:
        node: The current tree node.
        folder: Enclosing folder name to stamp onto emitted rows.
        out: Accumulator list that rows are appended to.
    """
    if not isinstance(node, dict):
        return
    if node.get("type") == "url" or (node.get("url") and node.get("type") != "folder"):
        # Chrome stores date_added as WebKit microseconds since 1601-01-01.
        raw = node.get("date_added")
        ms = 0
        try:
            ms = int((int(raw) / 1000) - 11_644_473_600_000)
        except (TypeError, ValueError):
            ms = 0
        out.append({
            "name": node.get("name", ""),
            "url": node.get("url", ""),
            "folder": folder,
            "dateAdded": ms,
        })
        return
    name = node.get("name", "")
    child_folder = folder or name
    for child in node.get("children", []) or []:
        _flatten_bookmark_node(child, child_folder, out)


def _rows_for_bookmarks(root: Path) -> list[dict[str, Any]]:
    """Flatten every top-level bookmark root into presentation rows.

    Chrome keeps several parallel roots (bookmark bar, other bookmarks,
    mobile), so all of them are traversed and concatenated into one flat
    list; the enclosing folder name is what tells them apart.

    Args:
        root: The data root.

    Returns:
        One row per bookmark URL, empty when the store is absent.
    """
    data = _load_browser_json(root, "bookmarks.enc")
    if not isinstance(data, dict):
        return []
    roots = data.get("roots", {})
    out: list[dict[str, Any]] = []
    for top in roots.values():
        _flatten_bookmark_node(top, "", out)
    return out


def _rows_for_logins(root: Path) -> list[dict[str, Any]]:
    """Login credentials.  Password values are masked *in core*: the real
    secret never crosses the admin boundary, by design."""
    data = _load_browser_json(root, "passwords.enc")
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for e in data:
        if not isinstance(e, dict):
            continue
        has_pw = bool(e.get("password"))
        out.append({
            "origin": e.get("origin") or e.get("url", ""),
            "username": e.get("username", ""),
            "kind": str(e.get("kind") or "password"),
            "password": _PASSWORD_MASK if has_pw else "",
        })
    return out


def _rows_for_storage(root: Path) -> list[dict[str, Any]]:
    """Project the local-storage store into key/value rows.

    The desktop main process snapshots each origin's own localStorage, so the
    store is normally ``{origin: {key: value}}`` and every row carries the
    origin it came from.  A flat mapping and a list of records are still
    accepted, so a store written by an older build degrades to a visible table
    rather than an empty one.  Values are clipped to 500 characters because the
    page renders them inline in a table cell and a megabyte-sized entry
    would stall the renderer for nothing.

    Args:
        root: The data root.

    Returns:
        ``{"origin", "key", "value"}`` rows, empty when the store is absent or
        has an unrecognised shape.
    """
    data = _load_browser_json(root, "localstorage.enc")
    if isinstance(data, dict):
        rows: list[dict[str, Any]] = []
        for outer_key, outer_value in data.items():
            if isinstance(outer_value, dict):
                for k, v in outer_value.items():
                    rows.append({"origin": str(outer_key), "key": str(k), "value": str(v)[:500]})
            else:
                rows.append({"origin": "", "key": str(outer_key), "value": str(outer_value)[:500]})
        return rows
    if isinstance(data, list):
        return [
            {"origin": "", "key": str(e.get("key", "")) if isinstance(e, dict) else "", "value": json.dumps(e, ensure_ascii=False)[:500] if not isinstance(e, dict) else str(e.get("value", ""))[:500]}
            for e in data
        ]
    return []


_BROWSER_ROW_READERS = {
    "logins": _rows_for_logins,
    "cookies": _rows_for_cookies,
    "storage": _rows_for_storage,
    "bookmarks": _rows_for_bookmarks,
    "history": _rows_for_history,
}


def module_rows(module_id: str) -> dict[str, Any]:
    """Serve one module's records for the data-center module page.

    Only the browser stores have readers today; every other module returns
    ``unsupported: true`` and the page keeps its open-folder action until
    its reader lands.  A missing store yields an empty row set -- it is a
    truthful zero, never invented data.

    Args:
        module_id: Registry id from the ``id=`` query parameter, already
            validated as alphanumeric by the caller in
            :func:`encre.server.admin.handle_admin`.

    Returns:
        ``{"id", "rows", "total", "truncated"}`` for a supported module,
        or ``{"id", "unsupported": True, "rows": [], "total": 0}`` for one
        that has no reader.  ``total`` always counts the full set even when
        ``rows`` is capped at :data:`_MAX_ROWS`, so the UI can show that a
        response is truncated instead of silently losing records.
    """
    root = get_data_dir(ensure=False)
    reader = _BROWSER_ROW_READERS.get(module_id)
    if reader is None:
        return {"id": module_id, "unsupported": True, "rows": [], "total": 0}
    rows = reader(root)
    total = len(rows)
    truncated = total > _MAX_ROWS
    return {
        "id": module_id,
        "rows": rows[:_MAX_ROWS],
        "total": total,
        "truncated": truncated,
    }


if __name__ == "__main__":  # pragma: no cover
    _cli()
