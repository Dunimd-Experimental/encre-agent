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

"""PDF reading tool for the Encre Agent tool system.

Extracts text and metadata from PDF documents. Two third-party libraries
are supported, tried in order:

1. ``pdfplumber`` -- preferred because it has the better text-extraction
   heuristics for multi-column and table-heavy documents.
2. ``PyPDF2`` -- used only if ``pdfplumber`` is unavailable. Both
   libraries are optional dependencies; the tool degrades gracefully to
   an ``"Error: No PDF library available..."`` string when neither can
   be imported, so the plugin module itself still imports cleanly on a
   minimal Encre Agent installation.

Design notes
------------

The library imports are deferred into ``_pdf_execute`` and wrapped in
``contextlib.suppress(ImportError)``. This lets test harnesses and ``py_compile``
touch the module without dragging in a heavy PDF dependency, and lets
the same plugin run on machines that only have one of the two libraries
installed.

Both extraction paths produce identical output shapes
(``"--- Page N ---\\n<text>"`` chunks joined with two newlines) so
callers do not need to know which library was selected underneath.
"""

from __future__ import annotations

import contextlib
from typing import Any

from encre.tools.base import build_tool


async def _pdf_execute(**kwargs: Any) -> str:
    """Execute a PDF read/extract/metadata request.

    The function is ``async`` even though the underlying library calls
    are blocking: ``build_tool`` expects an async callable so the
    dispatcher can ``await`` uniformly across all tools. Keeping the
    blocking I/O inside a plain ``try``/``except`` avoids threading
    complexity for a task that is bounded by a single file read.

    Args:
        kwargs: Tool input keyed by the JSON schema declared on
            ``pdf_tool.input_schema`` below.

    Returns:
        A string containing the extracted text or metadata. Errors are
        returned as ``"Error: ..."`` strings rather than raised so a
        missing file never kills an otherwise successful agent turn.
    """
    action = kwargs.get("action", "read")
    file_path = kwargs.get("file_path", "")
    pages = kwargs.get("pages", "")

    try:
        pdfplumber = None
        PdfReader = None  # noqa: N806
        with contextlib.suppress(ImportError):
            import pdfplumber

        if pdfplumber is None:
            with contextlib.suppress(ImportError):
                from PyPDF2 import PdfReader

        if pdfplumber is None and PdfReader is None:
            return (
                "Error: No PDF library available. "
                "Install one with: pip install pdfplumber or pip install PyPDF2"
            )

        if pdfplumber is not None:
            with pdfplumber.open(file_path) as pdf:
                if action == "metadata":
                    return str({"metadata": pdf.metadata, "total_pages": len(pdf.pages)})
                page_nums = _parse_pages(pages, len(pdf.pages))
                texts = []
                for pn in page_nums:
                    page = pdf.pages[pn - 1]
                    text = page.extract_text()
                    if text:
                        texts.append(f"--- Page {pn} ---\n{text}")
                return "\n\n".join(texts) if texts else "(no text extracted)"

        else:
            reader = PdfReader(file_path)
            num_pages = len(reader.pages)

            if action == "metadata":
                # PyPDF2 stores metadata as a dict of FieldInfo objects;
                # stringify each value so the output is a plain JSON-able
                # dict instead of a repr of ``FieldInfo`` wrappers.
                info = reader.metadata
                meta = {k: str(v) for k, v in info.items()} if info else {}
                meta["total_pages"] = num_pages
                return str(meta)

            page_nums = _parse_pages(pages, num_pages)
            texts = []
            for pn in page_nums:
                page = reader.pages[pn - 1]
                text = page.extract_text()
                if text:
                    texts.append(f"--- Page {pn} ---\n{text}")
            return "\n\n".join(texts) if texts else "(no text extracted)"

    except FileNotFoundError:
        return f"Error: File not found: {file_path}"
    except Exception as e:
        return f"Error processing PDF: {e}"


def _parse_pages(pages: str, num_pages: int) -> list[int]:
    """Parse a ``pages`` selector string into a sorted list of 1-indexed pages.

    Accepts the compact grammar advertised by the tool description:
    ``"1-5"`` for a range, ``"1,3,5"`` for a list, and any combination
    such as ``"1-3,7"``. When ``pages`` is empty, defaults to the first
    50 pages -- a hard cap that keeps an accidental full-document read on
    a 500-page PDF from blowing up the model's context window.

    Duplicates are de-duplicated via a ``set`` before sorting so
    overlapping ranges (e.g. ``"1-5,3-8"``) still produce a linear,
    ascending page list.

    Args:
        pages: The user-supplied selector. Empty string selects the
            default range (first 50 pages).
        num_pages: Total page count in the source PDF, used only to cap
            the default range.

    Returns:
        Sorted list of 1-indexed page numbers to read.
    """
    if not pages:
        end = min(num_pages, 50)
        return list(range(1, end + 1))
    result = []
    for part in pages.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            result.extend(range(int(a), int(b) + 1))
        else:
            result.append(int(part))
    return sorted(set(result))


pdf_tool = build_tool(
    name="pdf",
    description=(
        "Extract text or metadata from PDF files using pdfplumber (preferred) or "
        "PyPDF2 as a fallback. "
        "Use this to read document content, search text across pages, or inspect "
        "page count and metadata; prefer it over OCR when the source is a real text "
        "PDF. "
        "Do NOT use this for scanned/image-only PDFs (run them through an OCR tool), "
        "for editing PDFs, or for Office documents (use document/spreadsheet tools). "
        "Tips: pass a page range via `pages` (e.g. '1-5' or '1,3,5') to cap output "
        "size; omit it to read the first 50 pages. "
        "Pitfalls: scanned PDFs yield little or no text; large PDFs without a page "
        "range can produce very long responses."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "extract_text", "metadata"],
                "description": "PDF operation: 'read' or 'extract_text' returns per-page text, 'metadata' returns document metadata and total page count.",
            },
            "file_path": {
                "type": "string",
                "description": "Absolute or relative path to the PDF file to process.",
            },
            "pages": {
                "type": "string",
                "description": "1-indexed page selector: '1-5' for a range, '1,3,5' for a list. Defaults to the first 50 pages when omitted.",
            },
        },
        "required": ["action", "file_path"],
    },
    execute=_pdf_execute,
    intents=["data", "research"],
    category="docs",
    semantic_type="read",
    # PDF reading never mutates the source file, so concurrent reads are
    # safe even when the same file is targeted twice.
    is_concurrency_safe=lambda _: True,
    is_readonly=True,
)
