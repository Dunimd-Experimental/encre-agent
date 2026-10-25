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

"""Diagram rendering tool (Mermaid / Graphviz / PlantUML).

Converts textual diagram DSLs into rendered images (flowcharts,
sequence diagrams, UML, etc.) and returns the image path plus
diagnostics.

Supports three DSL formats:

* **Mermaid**: Rendered via the ``mmdc`` CLI when available, with an
  automatic fallback to the public ``mermaid.ink`` service. This means
  Mermaid diagrams work out of the box even without a local CLI install.
* **Graphviz**: Rendered via the ``dot`` CLI (Graphviz). Requires a
  local installation.
* **PlantUML**: Rendered via the ``plantuml`` CLI. Requires a local
  installation.

Two operations are supported:

* ``generate``: Render the DSL source to a PNG/SVG image.
* ``save_source``: Write the DSL source text to a file without
  rendering.
"""

import asyncio
import contextlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from encre.tools.base import build_tool
from encre.tools._encoding import decode_bytes


async def _diagram_execute(**kwargs: Any) -> str:
    """Execute the diagram tool and return a result message.

    This is the internal executor function bound to the ``diagram``
    tool's ``execute`` field. It dispatches to the appropriate handler
    based on the ``action`` and ``diagram_type`` parameters.

    The executor validates required fields before delegating to
    per-format handlers. Each handler manages its own subprocess
    lifecycle, timeout, and error reporting.

    Args:
        **kwargs: Arbitrary keyword arguments passed through by the
            tool dispatcher. Recognised keys:

            * ``action`` (str): Either ``"generate"`` or
              ``"save_source"``.
            * ``diagram_type`` (str): One of ``"mermaid"``,
              ``"graphviz"``, or ``"plantuml"``. Defaults to
              ``"mermaid"``.
            * ``source`` (str): The diagram DSL source code.
            * ``output_path`` (str): Destination file path for the
              rendered image or saved source.
            * ``width`` (int): Output image width in pixels
              (Mermaid only). Defaults to 0 (auto).
            * ``theme`` (str): Colour theme for Mermaid diagrams.
              Defaults to ``"default"``.

    Returns:
        A human-readable status message indicating success or failure,
        including the output file path when rendering succeeds.
    """
    action = kwargs.get("action", "")
    diagram_type = kwargs.get("diagram_type", "mermaid")
    source = kwargs.get("source", "")
    output_path = kwargs.get("output_path", "")
    width = kwargs.get("width", 0)
    theme = kwargs.get("theme", "default")

    if action == "generate":
        if not source:
            return "Missing required field: source"

        if diagram_type == "mermaid":
            return await _handle_mermaid(source, output_path, theme, width)
        elif diagram_type == "graphviz":
            return await _handle_graphviz(source, output_path, "png")
        elif diagram_type == "plantuml":
            return await _handle_plantuml(source, output_path)
        else:
            return f"Unsupported diagram type: {diagram_type}. Supported: mermaid, graphviz, plantuml"

    elif action == "save_source":
        if not source or not output_path:
            return "Missing required fields: source, output_path"
        try:
            p = Path(output_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(source, encoding="utf-8")
            return f"Diagram source saved to {output_path}"
        except Exception as e:
            return f"Failed to save diagram source: {e}"

    return f"Unknown action: {action}. Supported: generate, save_source"


def _mermaid_ink_render(source: str, theme: str, output_path: str) -> None:
    """Render Mermaid source via the public mermaid.ink service (no CLI).

    Used as a fallback when the ``mmdc`` CLI is not installed; writes
    SVG for ``.svg`` outputs and PNG otherwise. Raises on any
    transport/render failure.

    The Mermaid source is encoded as URL-safe base64 and passed as a
    path segment in the request URL, which avoids putting potentially
    large payloads in query parameters. The response is written
    directly to the output path.

    Args:
        source: The Mermaid DSL source code to render.
        theme: The colour theme name to apply (e.g. ``"default"``,
            ``"dark"``).
        output_path: The file path where the rendered image should be
            written.
    """
    import base64
    import json
    import urllib.request

    # Encode the Mermaid source as a state object, then base64-encode
    # it for URL-safe transport. The state format is the canonical
    # mermaid.ink input format.
    state = {"code": source, "mermaid": {"theme": theme or "default"}}
    payload = base64.urlsafe_b64encode(json.dumps(state).encode("utf-8")).decode("ascii")

    # The URL path segment ("svg" or "img") determines the response
    # content type; this is derived from the output file extension.
    kind = "svg" if output_path.lower().endswith(".svg") else "img"
    url = f"https://mermaid.ink/{kind}/{payload}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if not data:
        raise RuntimeError("mermaid.ink returned an empty response")
    Path(output_path).write_bytes(data)


async def _handle_mermaid(source: str, output_path: str, theme: str, width: int) -> str:
    """Render a Mermaid diagram using the mmdc CLI with fallbacks.

    Rendering strategy (in priority order):

    1. **mmdc CLI**: The preferred renderer. Writes the source to a
       temporary file, invokes ``mmdc``, and captures stdout/stderr.
    2. **mermaid.ink**: A public HTTP service that renders without any
       local dependencies. Runs in a thread executor to avoid blocking
       the event loop.
    3. **Save source only**: If both renderers fail, the DSL source is
       saved to disk so the user can render it manually later.

    Args:
        source: The Mermaid DSL source code to render.
        output_path: Destination file path for the rendered image.
        theme: The colour theme name to apply.
        width: Output image width in pixels (0 for auto).

    Returns:
        A human-readable status message indicating which renderer was
        used or why rendering failed.
    """
    tmp_dir_obj = None
    diagram_path = output_path or os.path.join(os.getcwd(), "diagram.png")
    try:
        # Use a temporary directory for the intermediate .mmd file so
        # we never leave partial files in the user's workspace.
        tmp_dir_obj = tempfile.TemporaryDirectory(prefix="encre_mermaid_")
        mmd_path = os.path.join(tmp_dir_obj.name, "diagram.mmd")
        Path(mmd_path).write_text(source, encoding="utf-8")

        # Ensure the output directory exists before writing the image.
        Path(diagram_path).parent.mkdir(parents=True, exist_ok=True)

        # Build the mmdc command line, appending optional flags.
        cmd = ["mmdc", "-i", mmd_path, "-o", diagram_path]
        if theme != "default":
            cmd.extend(["-t", theme])
        if width > 0:
            cmd.extend(["-w", str(width)])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

        if proc.returncode == 0:
            return f"Mermaid diagram rendered to {diagram_path}"
        mmdc_err = decode_bytes(stderr)[:300]
    except FileNotFoundError:
        mmdc_err = "mmdc CLI not installed"
    except subprocess.TimeoutExpired:
        return "Mermaid rendering timed out (60s). Source was saved."
    except Exception as e:
        mmdc_err = str(e)[:300]
    finally:
        # Always clean up the temporary directory, regardless of
        # whether the rendering succeeded or failed.
        if tmp_dir_obj is not None:
            with contextlib.suppress(Exception):
                tmp_dir_obj.cleanup()

    # Fallback 1: public mermaid.ink renderer (no local CLI required).
    # Runs in a thread executor to avoid blocking the event loop during
    # the synchronous HTTP request.
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _mermaid_ink_render, source, theme, diagram_path)
        return f"Mermaid diagram rendered to {diagram_path} (mermaid.ink fallback)"
    except Exception:
        pass

    # Fallback 2: keep the DSL source for later/manual rendering.
    # If the output path already has a .mmd extension, save there;
    # otherwise derive the .mmd path from the image path.
    src_path = output_path if output_path.lower().endswith(".mmd") else os.path.splitext(diagram_path)[0] + ".mmd"
    try:
        Path(src_path).parent.mkdir(parents=True, exist_ok=True)
        Path(src_path).write_text(source, encoding="utf-8")
    except OSError:
        pass
    return (
        f"Mermaid rendering unavailable ({mmdc_err}); source saved to {src_path}. "
        "Install mermaid-cli (npm i -g @mermaid-js/mermaid-cli) for local rendering."
    )


async def _handle_graphviz(source: str, output_path: str, output_format: str) -> str:
    """Render a Graphviz (DOT) diagram using the ``dot`` CLI.

    Invokes ``dot`` as a subprocess, piping the source text via stdin
    and capturing the rendered output. The ``dot`` command must be
    installed on the system (``apt install graphviz`` or
    ``brew install graphviz``).

    Args:
        source: The DOT language source code to render.
        output_path: Destination file path for the rendered image.
        output_format: The output image format (e.g. ``"png"``).

    Returns:
        A human-readable status message indicating success or failure.
    """
    try:
        if not output_path:
            output_path = os.path.join(os.getcwd(), "diagram.png")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # The dot CLI reads the graph definition from stdin and writes
        # the rendered output to the file specified by -o.
        proc = await asyncio.create_subprocess_exec(
            "dot", "-T" + output_format, "-o", output_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=source.encode("utf-8")), timeout=30
        )

        if proc.returncode == 0:
            return f"Graphviz diagram rendered to {output_path}"
        return f"Graphviz rendering failed: {decode_bytes(stderr)[:1000]}"
    except FileNotFoundError:
        return "Graphviz (dot) not found. Install graphviz (apt install graphviz or brew install graphviz)."
    except subprocess.TimeoutExpired:
        return "Graphviz rendering timed out."
    except Exception as e:
        return f"Failed to render Graphviz diagram: {e}"


async def _handle_plantuml(source: str, output_path: str) -> str:
    """Render a PlantUML diagram using the ``plantuml`` CLI.

    Invokes ``plantuml`` as a subprocess, writing the source to a
    temporary ``.puml`` file and specifying the output directory. The
    ``plantuml`` command must be installed on the system.

    The temporary ``.puml`` file is cleaned up in the ``finally``
    block regardless of whether the rendering succeeded.

    Args:
        source: The PlantUML source code to render.
        output_path: Destination file path for the rendered PNG.

    Returns:
        A human-readable status message indicating success or failure.
    """
    tmp_file = os.path.join(tempfile.gettempdir(), "encre_plantuml.puml")
    try:
        if not output_path:
            output_path = os.path.join(os.getcwd(), "diagram.png")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # Write the source to a temporary file; the plantuml CLI
        # expects a file argument rather than stdin.
        Path(tmp_file).write_text(source, encoding="utf-8")

        proc = await asyncio.create_subprocess_exec(
            "plantuml", "-tpng", tmp_file, "-o", str(Path(output_path).parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if Path(output_path).exists():
            return f"PlantUML diagram rendered to {output_path}"
        return f"PlantUML rendering failed: {decode_bytes(stderr)[:1000]}"
    except FileNotFoundError:
        return "PlantUML not found. Install from https://plantuml.com or use 'save_source' action."
    except subprocess.TimeoutExpired:
        return "PlantUML rendering timed out."
    except Exception as e:
        return f"Failed to render PlantUML diagram: {e}"
    finally:
        # Always clean up the temporary .puml file, even on failure.
        with contextlib.suppress(Exception):
            Path(tmp_file).unlink(missing_ok=True)


diagram_tool = build_tool(
    name="diagram",
    description=(
        "Render text-based diagrams (Mermaid, Graphviz/DOT, or PlantUML) into PNG/SVG "
        "images, or save the DSL source for later rendering. "
        "Use this to visualize flowcharts, sequence diagrams, architecture, or UML "
        "from DSL text; pick `diagram_type` to match the source syntax. "
        "Do NOT use this for quantitative data charts (use chart), mind maps drawn "
        "freehand, or live diagram editing in a GUI. "
        "Tips: Graphviz/PlantUML need their CLI installed (dot / plantuml); Mermaid "
        "renders via mmdc when present and otherwise auto-falls back to the public "
        "mermaid.ink service, so Mermaid works out of the box. Use `save_source` "
        "to keep only the DSL text. "
        "Pitfalls: rendering has a 30-60s timeout and falls back to saving source on "
        "failure - verify the returned message to confirm whether an image was "
        "actually produced."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["generate", "save_source"],
                "description": "Diagram operation: generate (render source to an image via the matching CLI) or save_source (write the DSL text to output_path without rendering).",
            },
            "diagram_type": {
                "type": "string",
                "enum": ["mermaid", "graphviz", "plantuml"],
                "description": "Diagram language matching the source syntax; drives which renderer CLI is invoked. Defaults to mermaid.",
            },
            "source": {"type": "string", "description": "Diagram source code in the chosen DSL (Mermaid, DOT, or PlantUML syntax); required for both generate and save_source."},
            "output_path": {"type": "string", "description": "Output file path for the rendered image (generate) or saved source (save_source); defaults to a temp file or working-directory file when omitted."},
            "width": {"type": "integer", "description": "Output image width in pixels; Mermaid only. Defaults to 0 (auto)."},
            "theme": {
                "type": "string",
                "enum": ["default", "dark", "neutral", "forest", "base"],
                "description": "Mermaid color theme; Mermaid only. Defaults to 'default'.",
            },
        },
        "required": ["action"],
    },
    execute=_diagram_execute,
    intents=["general", "coding", "data"],
    category="docs",
    semantic_type="generate",
    cost_level="low",
    retryability="auto",
    is_concurrency_safe=lambda _: True,
    is_destructive=lambda args: args.get("action", "") in ("generate", "save_source"),
)
