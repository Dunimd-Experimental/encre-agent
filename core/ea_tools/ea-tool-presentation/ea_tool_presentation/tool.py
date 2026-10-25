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

"""PowerPoint (``.pptx``) tool.

Creates, edits, and reads PowerPoint presentations using the
``python-pptx`` library. Supported actions:

* ``read``         -- dump every slide with its layout and text.
* ``extract_text`` -- return only the plain text of the deck.
* ``info``         -- return document metadata, slide dimensions
                      (in EMUs and inches), and core properties.
* ``create``       -- write a new ``.pptx`` with a title slide plus
                      zero or more content slides.
* ``add_slide``    -- append a single slide to an existing deck,
                      picking from the built-in layout index.
* ``list_slides``  -- return a per-slide summary (layout, shape count,
                      truncated title).

Design notes
------------

The ``python-pptx`` import is deferred inside each action handler
rather than at module level so that ``import ea_tool_presentation.tool``
succeeds on machines that have not installed ``python-pptx``. The
``_ensure_pptx`` helper runs ``importlib.util.find_spec("pptx")``
before each action and raises a friendly ``ImportError`` if the
library is missing, so the failure is visible at call time rather
than at import time.

Read-only actions (``read``, ``extract_text``, ``info``,
``list_slides``) never mutate the file on disk; write actions
(``create``, ``add_slide``) save in place and always go through
``prs.save(file_path)``. This split is what powers the tool-level
``is_concurrency_safe`` / ``is_destructive`` lambdas declared at the
bottom: only reads are concurrency-safe, and only writes are marked
destructive.

Layout selection in ``add_slide`` is a small fixed map from a
human-friendly name (``"blank"``, ``"title"``, ``"content"``,
``"two_content"``, ``"section_header"``) to the built-in layout index.
Custom template layouts are intentionally not addressable because
their ordering is not stable across ``python-pptx`` versions.
"""

import importlib
import json
import os
from typing import Any

from encre.tools.base import build_tool


async def _presentation_execute(**kwargs: Any) -> str:
    """Dispatch a presentation action by the ``action`` field on the input.

    The dispatcher is a straight if/elif chain because each branch
    only needs a different subset of ``kwargs`` and the actions share
    no meaningful pre-processing. ``file_path`` is required for every
    action except ``create`` because ``create`` derives a default
    filename from the title.

    Args:
        kwargs: Tool input keyed by the JSON schema declared on
            ``presentation_tool.input_schema`` below.

    Returns:
        A human-readable result string, or an ``"Error: ..."`` string
        when the required input is missing.
    """
    action = kwargs.get("action", "")
    file_path = kwargs.get("file_path", "")
    if not file_path and action not in ("create",):
        return "Error: 'file_path' is required."

    if action == "read":
        return _ppt_read(file_path)
    elif action == "extract_text":
        return _ppt_extract_text(file_path)
    elif action == "info":
        return _ppt_info(file_path)
    elif action == "create":
        title = kwargs.get("title", "Presentation")
        slides = kwargs.get("slides", [])
        return _ppt_create(file_path or f"{title.replace(' ', '_')}.pptx", title, slides)
    elif action == "add_slide":
        slide_type = kwargs.get("slide_type", "blank")
        content = kwargs.get("content", "")
        return _ppt_add_slide(file_path, slide_type, content)
    elif action == "list_slides":
        return _ppt_list_slides(file_path)
    else:
        return f"Error: Unknown action '{action}'."


def _ensure_pptx() -> None:
    """Raise ``ImportError`` if ``python-pptx`` is not installed.

    Called at the top of every action so the failure is attributed to
    the specific call the user is making, not to a module-level
    import. Uses ``importlib.util.find_spec`` rather than a bare
    ``import pptx`` because ``find_spec`` does not execute the module
    -- it only checks whether the loader can find it -- so this check
    is cheap and side-effect free.

    Raises:
        ImportError: When ``python-pptx`` cannot be imported. The
            message names the pip package so a caller can paste the
            install command straight out of the tool result.
    """
    if importlib.util.find_spec("pptx") is None:
        raise ImportError("python-pptx is required. Install with: pip install python-pptx")


def _ppt_read(file_path: str) -> str:
    """Return a full slide dump with layout names, text, and tables.

    Text frames are walked paragraph-by-paragraph so multi-line
    paragraphs stay on one line in the output; tables are
    ``" | "``-joined so they render as readable plain text in a chat
    transcript.

    Args:
        file_path: Path to the ``.pptx`` file to read.

    Returns:
        A ``"--- Slide N ---"``-delimited text dump, an
        ``"(empty presentation)"`` sentinel for a zero-slide deck, or
        an ``"Error: ..."`` string on failure.
    """
    _ensure_pptx()
    from pptx import Presentation
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    try:
        prs = Presentation(file_path)
        parts = []
        for i, slide in enumerate(prs.slides):
            parts.append(f"\n--- Slide {i + 1} ---")
            layout_name = slide.slide_layout.name if slide.slide_layout else "unknown"
            parts.append(f"Layout: {layout_name}")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        t = para.text.strip()
                        if t:
                            parts.append(t)
                if shape.has_table:
                    table = shape.table
                    for row in table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        parts.append(" | ".join(cells))
        return "\n".join(parts) if len(parts) > 1 else "(empty presentation)"
    except Exception as e:
        return f"Error reading presentation: {e}"


def _ppt_extract_text(file_path: str) -> str:
    """Return only the plain text of a deck, one paragraph per line.

    Deliberately does NOT include layout names, tables, or slide
    separators: the caller is asking for the content stream only,
    typically for feeding downstream text processing. Text frames are
    walked paragraph-by-paragraph so multi-line paragraphs stay on
    one line in the output.

    Args:
        file_path: Path to the ``.pptx`` file to extract text from.

    Returns:
        A newline-joined text stream, an ``"(no text content)"``
        sentinel when the deck has no text frames, or an
        ``"Error: ..."`` string on failure.
    """
    _ensure_pptx()
    from pptx import Presentation
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    try:
        prs = Presentation(file_path)
        texts = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        t = para.text.strip()
                        if t:
                            texts.append(t)
        return "\n".join(texts) if texts else "(no text content)"
    except Exception as e:
        return f"Error extracting text: {e}"


def _ppt_info(file_path: str) -> str:
    """Return presentation metadata as JSON.

    Slide dimensions are reported twice: once in raw EMUs (English
    Metric Units, python-pptx's internal unit) and once converted to
    inches using the ``1 inch = 914400 EMU`` factor. Reporting both
    lets the caller decide which unit is convenient for their use
    case (e.g. computing a thumbnail aspect ratio) without doing the
    conversion themselves.

    Args:
        file_path: Path to the ``.pptx`` file to introspect.

    Returns:
        JSON-encoded metadata dict, or an ``"Error: ..."`` string on
        failure.
    """
    _ensure_pptx()
    from pptx import Presentation
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    try:
        prs = Presentation(file_path)
        slide_width = prs.slide_width
        slide_height = prs.slide_height
        core_props = prs.core_properties
        return json.dumps({
            "file": os.path.basename(file_path),
            "size_bytes": os.path.getsize(file_path),
            "slides": len(prs.slides),
            "slide_width_emu": slide_width,
            "slide_height_emu": slide_height,
            "slide_width_inches": round(slide_width / 914400, 1),
            "slide_height_inches": round(slide_height / 914400, 1),
            "author": str(core_props.author or ""),
            "title": str(core_props.title or ""),
            "created": str(core_props.created or ""),
            "modified": str(core_props.modified or ""),
        }, indent=2)
    except Exception as e:
        return f"Error reading presentation info: {e}"


def _ppt_create(file_path: str, title: str, slides: list) -> str:
    """Create a new ``.pptx`` from a title and a list of slide specs.

    Each entry in ``slides`` is either a plain string (becomes the
    title of a content slide) or a dict with ``title`` and ``content``
    keys (the ``content`` value is written to the first non-title
    placeholder). The first slide is always the title slide taken
    from layout index 0; every subsequent slide uses layout index 1
    (title + content).

    Args:
        file_path: Destination path. Parent directories must already
            exist; this function does not create them.
        title: Text of the first (title) slide.
        slides: Iterable of slide specs. Each item is a ``str``
            (slide title only) or a ``dict`` with ``title`` and
            optional ``content``. Non-string, non-dict items are
            silently skipped.

    Returns:
        A ``"Presentation created: <abspath> (N slides)"`` string, or
        an ``"Error: ..."`` string on failure.
    """
    _ensure_pptx()
    from pptx import Presentation

    try:
        prs = Presentation()
        prs.slides.add_slide(prs.slide_layouts[0])
        title_shape = prs.slides[0].shapes.title
        if title_shape:
            title_shape.text = title

        if isinstance(slides, list):
            for slide_data in slides:
                if isinstance(slide_data, str):
                    slide = prs.slides.add_slide(prs.slide_layouts[1])
                    if slide.shapes.title:
                        slide.shapes.title.text = slide_data
                elif isinstance(slide_data, dict):
                    slide = prs.slides.add_slide(prs.slide_layouts[1])
                    slide_title = slide_data.get("title", "")
                    content = slide_data.get("content", "")
                    if slide.shapes.title:
                        slide.shapes.title.text = slide_title
                    if content and slide.placeholders and len(slide.placeholders) > 1:
                        tf = slide.placeholders[1].text_frame
                        tf.text = content

        prs.save(file_path)
        return f"Presentation created: {os.path.abspath(file_path)} ({len(prs.slides)} slides)"
    except Exception as e:
        return f"Error creating presentation: {e}"


def _ppt_add_slide(file_path: str, slide_type: str, content: str) -> str:
    """Append one slide to an existing deck.

    Layout is chosen from a fixed map of built-in indices:
    ``blank=6``, ``title=0``, ``content=1``, ``two_content=3``,
    ``section_header=2``. The first line of ``content`` is written to
    the slide title; any subsequent lines are inserted as paragraphs
    into the first non-title text frame.

    Args:
        file_path: Path to the existing ``.pptx`` to append to.
        slide_type: One of ``blank``, ``title``, ``content``,
            ``two_content``, ``section_header``. Unknown values fall
            back to ``blank``.
        content: Multi-line body. The first line becomes the slide
            title; subsequent lines become bullet paragraphs in the
            first non-title text frame.

    Returns:
        A ``"Slide added to <basename> (layout: <type>)"`` string, or
        an ``"Error: ..."`` string on failure.
    """
    _ensure_pptx()
    from pptx import Presentation
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    try:
        prs = Presentation(file_path)
        layout_map = {
            "blank": prs.slide_layouts[6],
            "title": prs.slide_layouts[0],
            "content": prs.slide_layouts[1],
            "two_content": prs.slide_layouts[3],
            "section_header": prs.slide_layouts[2],
        }
        layout = layout_map.get(slide_type, prs.slide_layouts[6])
        slide = prs.slides.add_slide(layout)

        if content and slide.shapes.title:
            slide.shapes.title.text = content.split("\n")[0] if "\n" in content else content

        if content and "\n" in content:
            body_lines = content.split("\n")[1:]
            for shape in slide.shapes:
                if shape.has_text_frame and shape != slide.shapes.title:
                    tf = shape.text_frame
                    tf.text = ""
                    for i, line in enumerate(body_lines):
                        line = line.strip()
                        if not line:
                            continue
                        if i == 0:
                            tf.text = line
                        else:
                            p = tf.add_paragraph()
                            p.text = line

        prs.save(file_path)
        return f"Slide added to {os.path.basename(file_path)} (layout: {slide_type})"
    except Exception as e:
        return f"Error adding slide: {e}"


def _ppt_list_slides(file_path: str) -> str:
    """Return a compact per-slide summary.

    For each slide, reports the layout name, shape count, and the
    first 60 characters of the title text. The 60-char truncation
    keeps the output readable when the deck is long; a full title
    dump is available via the ``read`` action.

    Args:
        file_path: Path to the ``.pptx`` to summarise.

    Returns:
        A ``"Total slides: N"`` header followed by one line per slide,
        a ``"No slides in presentation."`` sentinel for empty decks,
        or an ``"Error: ..."`` string on failure.
    """
    _ensure_pptx()
    from pptx import Presentation
    if not os.path.isfile(file_path):
        return f"Error: File not found: {file_path}"
    try:
        prs = Presentation(file_path)
        if not prs.slides:
            return "No slides in presentation."
        out = [f"Total slides: {len(prs.slides)}"]
        for i, slide in enumerate(prs.slides):
            layout = slide.slide_layout.name if slide.slide_layout else "unknown"
            shape_count = len(slide.shapes)
            title_text = ""
            if slide.shapes.title:
                title_text = slide.shapes.title.text[:60]
            out.append(f"  Slide {i+1}: layout={layout}, shapes={shape_count}, title={title_text!r}")
        return "\n".join(out)
    except Exception as e:
        return f"Error listing slides: {e}"


presentation_tool = build_tool(
    name="presentation",
    description=(
        "Read, create, and edit PowerPoint (.pptx) presentations via python-pptx, "
        "including slides, titles, bullet bodies, and tables. "
        "Use this for `read` (full slide dump), `extract_text` (plain text), `info` "
        "(metadata), `create` (new deck with optional slides), `add_slide` (append), "
        "or `list_slides` (overview). "
        "Do NOT use this for Word documents (use document) or PDFs (use pdf); and "
        "avoid it for .odp or Keynote files. "
        "Tips: for `create`, pass `slides` as a list of strings or {title, content} "
        "dicts; for `add_slide` use `slide_type` to pick a layout and put the title "
        "on the first `content` line. "
        "Pitfalls: requires python-pptx installed; `add_slide` reuses built-in layouts "
        "and may not match custom template layouts."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "extract_text", "info", "create", "add_slide", "list_slides"],
                "description": "Presentation operation: read (full content), extract_text (plain text), info (metadata), create (new deck), add_slide (append), or list_slides (overview).",
            },
            "file_path": {
                "type": "string",
                "description": "Path to the .pptx file; required for all actions except create.",
            },
            "title": {
                "type": "string",
                "description": "Title used on the title slide when action=create.",
            },
            "slides": {
                "type": "array",
                "description": "Initial slides for create; each item is a string (slide title) or a dict with 'title' and 'content' keys.",
            },
            "slide_type": {
                "type": "string",
                "enum": ["blank", "title", "content", "two_content", "section_header"],
                "description": "Layout to apply when adding a slide via add_slide.",
            },
            "content": {
                "type": "string",
                "description": "Body for add_slide; the first line becomes the slide title and subsequent lines become bullet points.",
            },
        },
        "required": ["action"],
    },
    execute=_presentation_execute,
    intents=["data", "general", "research"],
    # Read-only actions share the source file; write actions mutate it.
    # Only the reads are safe to interleave.
    is_concurrency_safe=lambda data: data.get("action") in ("read", "extract_text", "info", "list_slides"),
    # Only write actions touch the file on disk. Reads and info are
    # safe to run without user confirmation.
    is_destructive=lambda args: args.get("action", "") in ("create", "add_slide"),
    category="docs",
    semantic_type="generate",
)
