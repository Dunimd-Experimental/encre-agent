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

"""EA File Read Tool - mandatory builtin package."""

import base64
import os
from typing import Any

from encre.native import read_file as _native_read
from encre.tools.base import build_tool
from encre.tools.path_guard import guard_tool_path
from encre.tools.tool_ui import ui_dict

_IMAGE_EXTS = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

_IMAGE_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"), (b"BM", "image/bmp"),
]


def _detect_image_mime(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXTS:
        return _IMAGE_EXTS[ext]
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    for magic, mime in _IMAGE_MAGIC:
        if mime == "image/webp":
            if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
                return mime
        elif head.startswith(magic):
            return mime
    return None


def _is_pdf(path: str) -> bool:
    if os.path.splitext(path)[1].lower() == ".pdf":
        return True
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def _read_pdf_text(path: str, max_pages: int | None) -> str:
    try:
        import pypdf
    except ImportError:
        try:
            import PyPDF2 as pypdf  # noqa: N813
        except ImportError as exc:
            raise RuntimeError("PDF reading requires 'pypdf' or 'PyPDF2'. pip install pypdf") from exc
    reader = pypdf.PdfReader(path)
    out: list[str] = []
    total = len(reader.pages)
    limit = total if not max_pages else min(total, max_pages)
    for i in range(limit):
        try:
            txt = reader.pages[i].extract_text() or ""
        except Exception:
            txt = ""
        out.append(f"--- page {i + 1} ---\n{txt}")
    if limit < total:
        out.append(f"\n... ({total - limit} more page(s) not shown)")
    return "\n\n".join(out)


async def _file_read_execute(**kwargs: Any) -> str:
    file_path = kwargs.get("file_path", "")
    if not file_path:
        return "Error: file_path is required"
    guard = guard_tool_path(file_path, tool="file_read", for_write=False)
    if not guard.allowed:
        return f"Error: {guard.reason}"
    file_path = guard.resolved
    if not os.path.exists(file_path):
        return f"Error: File not found: {file_path}"
    if os.path.isdir(file_path):
        return f"Error: Path is a directory, not a file: {file_path}"

    as_image = bool(kwargs.get("as_image", False))
    mime = _detect_image_mime(file_path) if (as_image or _detect_image_mime(file_path) is not None) else None
    if mime is not None:
        try:
            with open(file_path, "rb") as fh:
                data = fh.read()
        except PermissionError:
            return f"Error: Permission denied: {file_path}"
        except Exception as e:
            return f"Error reading image: {e}"
        import json
        return json.dumps({"type": "image", "path": file_path, "mime": mime,
                           "size_bytes": len(data), "base64": base64.b64encode(data).decode("ascii")},
                          ensure_ascii=False)

    if _is_pdf(file_path):
        try:
            text = _read_pdf_text(file_path, kwargs.get("max_pages"))
        except RuntimeError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            return f"Error reading PDF: {exc}"
        return text

    limit = int(kwargs.get("limit", 0) or 0)
    offset = int(kwargs.get("offset", 1) or 1)
    try:
        content = _native_read(file_path, offset, limit)
    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except PermissionError:
        return f"Error: Permission denied: {file_path}"
    except UnicodeDecodeError:
        size = os.path.getsize(file_path) if os.path.exists(file_path) else -1
        return f"Error: binary file ({size} bytes). Use as_image=true."
    except Exception as e:
        return f"Error reading file: {e}"
    if not content:
        return f"(empty file, {os.path.getsize(file_path)} bytes)"
    return content


file_read_tool = build_tool(
    name="file_read",
    description=(
        "Read a file from the local filesystem. Text files return numbered lines; "
        "images return a base64 envelope; PDFs return extracted text. "
        "Use instead of cat/head/tail in bash. Supports offset/limit pagination, "
        "as_image for images/PDF pages, and max_pages for PDF caps."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the file to read (required)."},
            "limit": {"type": "integer", "description": "Max lines to read (optional). Omit for full file."},
            "offset": {"type": "integer", "description": "1-indexed line to start from (optional, default 1)."},
            "as_image": {"type": "boolean", "description": "Force base64 image output (auto-detected for images)."},
            "max_pages": {"type": "integer", "description": "PDF page cap (optional)."},
        },
        "required": ["file_path"],
    },
    execute=_file_read_execute,
    ui=ui_dict(icon="eye", label="Read File", card="item", result="file").as_dict(),
    intents=["general", "coding", "data"],
    category="filesystem",
    triggers=["read file", "cat", "view file", "open file"],
    semantic_type="read", cost_level="low", retryability="auto",
    is_concurrency_safe=lambda _: True, is_readonly=lambda _: True,
)
