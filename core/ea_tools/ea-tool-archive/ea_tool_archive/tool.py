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

"""Module: ea_tool_archive.tool

Archive / compression tool supporting zip, tar, gz, bz2, and xz formats.

The tool exposes four actions (``create``, ``extract``, ``list``, ``info``)
over common archive formats behind a single tool interface. All blocking
I/O is dispatched to a thread-pool executor so the async tool runner never
stalls the event loop. Each action is implemented as a plain function so it
can be run synchronously inside ``loop.run_in_executor``.

Supported formats:

- ``zip`` -- Python ``zipfile`` (deflate compression).
- ``tar`` -- uncompressed tar stream (Python ``tarfile``).
- ``gz`` / ``bz2`` / ``xz`` -- gzip / bzip2 / xz compressed tar streams.

Security note: ``extract`` unpacks archives as-is. Archive entries can
contain absolute or ``..`` paths ("zip-slip"), so callers should inspect
with ``action='list'`` before extracting untrusted archives into sensitive
directories. This module does not filter entries; the sandbox layer is
responsible for path confinement if one is configured.
"""

from __future__ import annotations

import asyncio
import json
import os
import tarfile
import zipfile
from pathlib import Path
from typing import Any

from encre.tools.base import build_tool


async def _archive_execute(**kwargs: Any) -> str:
    """Dispatch to the requested archive action.

    Args:
        kwargs: Tool call arguments. Recognised keys are ``action``,
            ``archive_path``, ``source_paths``, ``dest_dir``, ``format``,
            and ``compression``. Unknown keys are ignored.

    Returns:
        A human-readable status string on success or a machine-parseable
        error string on failure. The ``list`` and ``info`` actions return
        JSON-encoded payloads so downstream agents can consume structured
        metadata without extra parsing.

    Blocking I/O is offloaded to the default thread-pool executor because
    both ``zipfile`` and ``tarfile`` perform synchronous reads/writes that
    would otherwise stall the event loop and starve sibling tool calls.
    """
    action = kwargs.get("action", "")
    archive_path = kwargs.get("archive_path", "")
    source_paths = kwargs.get("source_paths", [])
    dest_dir = kwargs.get("dest_dir", "")
    archive_format = kwargs.get("format", "zip")
    compression = kwargs.get("compression", "")

    # ``archive_path`` is required for every action -- failing fast here
    # avoids running an executor job that would just return a confusing
    # "missing path" error from the underlying library.
    if not archive_path:
        return "Missing required field: archive_path"

    loop = asyncio.get_event_loop()

    if action == "create":
        if not source_paths:
            return "Missing required field: source_paths"

        def _create() -> str:
            """Create an archive from the given source files / directories.

            The ``zip`` branch uses ``zipfile.ZipFile`` with deflate
            compression and preserves each source's path relative to its
            parent, so extracting the archive reproduces the original
            directory layout. The tar-family branch selects the tarfile
            write mode based on the requested compression (``gz``/``bz2``/
            ``xz``) with the file extension as a fallback hint.
            """
            try:
                archive_path_p = Path(archive_path)
                archive_path_p.parent.mkdir(parents=True, exist_ok=True)

                if archive_format == "zip":
                    with zipfile.ZipFile(str(archive_path_p), "w", zipfile.ZIP_DEFLATED) as zf:
                        for sp in source_paths:
                            sp_path = Path(sp)
                            if sp_path.is_dir():
                                # Store relative to the parent so the
                                # archive contains the top-level directory
                                # name, matching tar's default behaviour.
                                for file_path in sp_path.rglob("*"):
                                    if file_path.is_file():
                                        arcname = str(file_path.relative_to(sp_path.parent))
                                        zf.write(str(file_path), arcname)
                            elif sp_path.is_file():
                                zf.write(str(sp_path), sp_path.name)
                elif archive_format in ("tar", "gz", "bz2", "xz"):
                    ext = archive_format
                    # Pick the tarfile mode from either the explicit
                    # compression override or the format-derived extension.
                    if compression == "gz" or ext == "gz":
                        mode = "w:gz"
                    elif compression == "bz2" or ext == "bz2":
                        mode = "w:bz2"
                    elif compression == "xz" or ext == "xz":
                        mode = "w:xz"
                    else:
                        # Default: uncompressed tar when the format says so,
                        # otherwise fall back to gzip (a safe general choice).
                        mode = "w" if ext == "tar" else "w:gz"
                    with tarfile.open(str(archive_path_p), mode) as tf:
                        for sp in source_paths:
                            sp_path = Path(sp)
                            if sp_path.is_dir() or sp_path.is_file():
                                tf.add(str(sp_path), arcname=sp_path.name)
                else:
                    return f"Unsupported format: {archive_format}. Supported: zip, tar, gz, bz2, xz"

                return f"Archive created: {archive_path_p} ({_size_str(archive_path_p.stat().st_size)})"
            except Exception as e:
                return f"Failed to create archive: {e}"

        return await loop.run_in_executor(None, _create)

    elif action == "extract":
        if not dest_dir:
            dest_dir = os.path.dirname(archive_path)

        def _extract() -> str:
            """Extract the archive into ``dest_dir``.

            We sniff the format first via ``zipfile.is_zipfile`` so a plain
            ``.tar.gz`` archive is routed to the tarfile reader even if the
            caller passes a non-standard extension. Directory creation is
            idempotent -- callers can point ``dest_dir`` at an existing
            folder and entries will be merged.
            """
            try:
                dest_path = Path(dest_dir)
                dest_path.mkdir(parents=True, exist_ok=True)

                if zipfile.is_zipfile(archive_path):
                    with zipfile.ZipFile(archive_path, "r") as zf:
                        zf.extractall(str(dest_path))
                else:
                    if not tarfile.is_tarfile(archive_path):
                        return f"Not a valid archive: {archive_path}"
                    with tarfile.open(archive_path, "r:*") as tf:
                        tf.extractall(str(dest_path))

                # Count everything the walk sees so the caller gets a
                # single summary figure rather than having to re-stat.
                extracted = sum(len(files) for _, _, files in os.walk(str(dest_path)))
                return f"Extracted to {dest_path} ({extracted} files)"
            except Exception as e:
                return f"Failed to extract archive: {e}"

        return await loop.run_in_executor(None, _extract)

    elif action == "list":
        def _list() -> str:
            """Return the archive's entry metadata as pretty JSON.

            For zip files we read the ZIP central directory (``infolist``)
            which exposes both uncompressed and compressed sizes plus the
            mod-time encoded by the archive. For tar files, ``getmembers``
            returns one entry per member; we translate ``isdir()`` into a
            readable ``type`` field and expose the tar mtime as a plain
            Unix epoch for consumers.
            """
            try:
                entries = []

                if zipfile.is_zipfile(archive_path):
                    with zipfile.ZipFile(archive_path, "r") as zf:
                        for info in zf.infolist():
                            entries.append({
                                "name": info.filename,
                                "size": info.file_size,
                                "compressed": info.compress_size,
                                "date": _fmt_date(info.date_time),
                            })
                else:
                    with tarfile.open(archive_path, "r:*") as tf:
                        for member in tf.getmembers():
                            entries.append({
                                "name": member.name,
                                "size": member.size,
                                "type": "directory" if member.isdir() else "file",
                                "date": member.mtime,
                            })
                return json.dumps(entries, ensure_ascii=False, indent=2)
            except Exception as e:
                return f"Failed to list archive: {e}"

        return await loop.run_in_executor(None, _list)

    elif action == "info":
        def _info() -> str:
            """Return archive-level metadata (size, entry count, format).

            Format detection prefers the caller-supplied ``archive_format``
            value and only falls back to the file extension when the caller
            omitted it, so the reported format is stable even for
            extensionless archives.
            """
            try:
                p = Path(archive_path)
                if not p.exists():
                    return f"File not found: {archive_path}"
                stat = p.stat()
                count = 0
                if zipfile.is_zipfile(archive_path):
                    with zipfile.ZipFile(archive_path, "r") as zf:
                        count = len(zf.infolist())
                else:
                    with tarfile.open(archive_path, "r:*") as tf:
                        count = len(tf.getmembers())
                ext = str(archive_path).lower()
                info_data = {
                    "path": str(p),
                    "size": stat.st_size,
                    "size_str": _size_str(stat.st_size),
                    "entries": count,
                    "format": archive_format if archive_format else ext.rsplit(".", 1)[-1],
                    "modified": stat.st_mtime,
                }
                return json.dumps(info_data, ensure_ascii=False, indent=2)
            except Exception as e:
                return f"Failed to get archive info: {e}"

        return await loop.run_in_executor(None, _info)

    return f"Unknown action: {action}. Supported: create, extract, list, info"


def _size_str(size_bytes: int) -> str:
    """Return a human-readable size string (e.g. ``"1.2 MB"``).

    The conversion divides by 1024 per step and stops at the first unit
    whose quotient is below 1024. Values above 1024 TB fall through to a
    TB-labelled string; that range is effectively unreachable for
    practical archives so we do not bother adding a PB bucket.
    """
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def _fmt_date(dt: tuple[int, ...]) -> str:
    """Format a ``(year, month, day, ...)`` tuple as ``YYYY-MM-DD``.

    ZIP archives encode an epoch of 1980-01-01 and store a zero year when
    the timestamp was not preserved; we return an empty string in that
    case rather than a nonsense ``0000-00-00`` date.
    """
    if not dt or dt[0] == 0:
        return ""
    return f"{dt[0]:04d}-{dt[1]:02d}-{dt[2]:02d}"


archive_tool = build_tool(
    name="archive",
    description=(
        "Create, extract, list, or inspect archives in zip, tar, gz, bz2, or "
        "xz format. Use this instead of bash `tar`/`zip`/`unzip` -- it "
        "provides a single interface across formats, returns structured JSON "
        "for list/info actions, and handles directory recursion. Create from "
        "files or directories, extract to a destination, list entries, or get "
        "archive metadata (size, entry count, format). "
        "TIP: For 'create', set 'format' explicitly to avoid ambiguity from "
        "the file extension. "
        "AVOID: Extracting untrusted archives with action='extract' into "
        "sensitive directories -- inspect with action='list' first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "extract", "list", "info"],
                "description": "Archive action (required). 'create' builds a new archive; 'extract' unpacks; 'list' returns entry metadata; 'info' returns archive-level metadata.",
            },
            "archive_path": {"type": "string", "description": "Path to the archive file (required). For 'create', parent directories are created automatically."},
            "source_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of files or directories to archive (required for action='create'). Directories are added recursively.",
            },
            "dest_dir": {"type": "string", "description": "Destination directory for extraction (optional for action='extract'; defaults to the archive's parent directory)."},
            "format": {
                "type": "string",
                "enum": ["zip", "tar", "gz", "bz2", "xz"],
                "description": "Archive format for action='create' (optional, default 'zip').",
            },
            "compression": {
                "type": "string",
                "enum": ["", "gz", "bz2", "xz"],
                "description": "Compression type for tar archives (optional). Overrides the format-implied compression.",
            },
        },
        "required": ["action", "archive_path"],
    },
    execute=_archive_execute,
    intents=["general", "coding", "data"],
    category="filesystem",
    semantic_type="write",
    cost_level="low",
    retryability="auto",
    # Archive I/O is not concurrency-safe: parallel create/extract calls
    # can race on the destination path and corrupt the entry list.
    is_concurrency_safe=lambda _: False,
    is_destructive=lambda args: args.get("action") in ("create", "extract"),
)
