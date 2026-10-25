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

"""Image generation, editing, and variation tools for the Encre tool system.

This module exposes three tools built on top of the active backend's
multimodal capabilities (DALL-E-compatible):

- :data:`generate_image_tool` -- create new images from a text prompt.
- :data:`edit_image_tool` -- inpaint an existing image with a text
  prompt, optionally constrained to a masked region.
- :data:`image_variation_tool` -- generate alternative variations of
  a source image without a text prompt.

Each tool is deliberately a thin adapter over the backend: the module
does not implement the model call itself, it just defers to whichever
backend is currently attached to the agent loop. The capability gate
(:func:`_get_multimodal_blocker`) is evaluated up front so an LLM that
has already decided to call the tool gets a friendly error message
rather than a cryptic AttributeError from the backend.
"""

import json
from typing import Any

from encre.tools.base import build_tool
from encre.backends.multimodal import MultimodalMixin


def _get_backend() -> MultimodalMixin | None:
    """Return the active backend if it supports image generation.

    The import of :func:`_resolve_loop` is deferred to call time so
    that merely importing this module does not pull in the whole
    runtime module.
    """
    from encre.tools.runtime import _resolve_loop
    loop = _resolve_loop()
    if loop and hasattr(loop.backend, "generate_image"):
        return loop.backend
    return None


def _get_multimodal_blocker() -> str | None:
    """Return a friendly error when the active model cannot generate images,
    else ``None``.

    Uses the per-endpoint ``images_generation`` capability recorded by
    the background probe (``"unsupported"`` blocks, ``"supported"``
    allows). Falls back to the legacy ``multimodal_support`` gate when
    the node-level capability has not been probed yet.

    Returning ``None`` in every ambiguous case is deliberate: it is
    better to let the backend raise and the caller surface a real
    error than to block a model that actually works just because the
    probe has not finished yet.
    """
    from encre.tools.runtime import _resolve_loop
    loop = _resolve_loop()
    config = getattr(loop, "config", None) if loop is not None else None
    if config is None or not hasattr(config, "get_active_model"):
        return None
    try:
        model = config.get_active_model()
    except Exception:
        return None
    if model is None:
        return None
    node_cap = None
    capabilities = getattr(model, "capabilities", None)
    if isinstance(capabilities, dict):
        node_cap = capabilities.get("images_generation")
    if node_cap == "supported":
        return None
    if node_cap == "unsupported" or getattr(model, "multimodal_support", "unknown") == "unsupported":
        return (
            "The active model's endpoint does not support image generation "
            "(capability probe reported unsupported)."
        )
    return None


async def _generate_image_execute(**kwargs: Any) -> str:
    """Run a text-to-image generation on the active backend.

    Wraps the backend's async ``generate_image`` coroutine and
    serialises the result to JSON. Results that support
    :meth:`~object.to_dict` (dataclass-style result objects) are
    converted to plain dicts; anything else is serialised with
    ``default=str`` as a fallback so an unexpected return type does
    not crash the tool.

    Args:
        **kwargs: Tool arguments. ``prompt`` (required), ``n``
            (default 1), ``size`` (default ``1024x1024``).

    Returns:
        A JSON string describing the generated image(s), or an
        ``Error:``-prefixed string on failure.
    """
    blocker = _get_multimodal_blocker()
    if blocker:
        return f"Error: {blocker}"
    backend = _get_backend()
    if backend is None:
        return "Error: Backend does not support image generation"
    try:
        prompt = kwargs.get("prompt", "")
        n = int(kwargs.get("n", 1))
        size = kwargs.get("size", "1024x1024")
        result = await backend.generate_image(prompt=prompt, n=n, size=size)
        return json.dumps(result.to_dict() if hasattr(result, "to_dict") else result, indent=2, default=str)
    except Exception as e:
        return f"Error generating image: {e}"


async def _edit_image_execute(**kwargs: Any) -> str:
    """Inpaint an existing image on the active backend.

    Args:
        **kwargs: Tool arguments. ``image`` (required -- base64 or URL),
            ``prompt`` (required), ``mask`` (optional -- transparent
            pixels mark the editable region).

    Returns:
        A JSON string describing the edited image, or an
        ``Error:``-prefixed string on failure.
    """
    blocker = _get_multimodal_blocker()
    if blocker:
        return f"Error: {blocker}"
    backend = _get_backend()
    if backend is None:
        return "Error: Backend does not support image editing"
    try:
        image = kwargs.get("image", "")
        prompt = kwargs.get("prompt", "")
        mask = kwargs.get("mask", "")
        result = await backend.edit_image(image=image, prompt=prompt, mask=mask)
        return json.dumps(result.to_dict() if hasattr(result, "to_dict") else result, indent=2, default=str)
    except Exception as e:
        return f"Error editing image: {e}"


async def _image_variation_execute(**kwargs: Any) -> str:
    """Generate variations of a source image on the active backend.

    Args:
        **kwargs: Tool arguments. ``image`` (required -- base64 or URL),
            ``n`` (default 1), ``size`` (default ``1024x1024``).

    Returns:
        A JSON string describing the generated variations, or an
        ``Error:``-prefixed string on failure.
    """
    blocker = _get_multimodal_blocker()
    if blocker:
        return f"Error: {blocker}"
    backend = _get_backend()
    if backend is None:
        return "Error: Backend does not support image variation"
    try:
        image = kwargs.get("image", "")
        n = int(kwargs.get("n", 1))
        size = kwargs.get("size", "1024x1024")
        result = await backend.create_image_variation(image=image, n=n, size=size)
        return json.dumps(result.to_dict() if hasattr(result, "to_dict") else result, indent=2, default=str)
    except Exception as e:
        return f"Error creating image variation: {e}"


generate_image_tool = build_tool(
    name="generate_image",
    description=(
        "Generate one or more images from a text prompt using the active backend's "
        "image-generation model (DALL-E-compatible). "
        "Use this to create original artwork, illustrations, or visual assets from a "
        "description; prefer it over diagram tools for photorealistic or painterly "
        "output. "
        "Do NOT use this for editing an existing image (use edit_image), for "
        "diagrams/flowcharts (use diagram), or for data charts (use chart). "
        "Tips: be specific about style, composition, and aspect ratio in the prompt; "
        "check supported `size` values for the target backend. "
        "Pitfalls: the active backend must implement generate_image, otherwise the "
        "call returns an unsupported error."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Text description of the image to generate; include style, subject, composition, and mood for best results."},
            "n": {"type": "integer", "description": "Number of images to generate; defaults to 1.", "default": 1},
            "size": {"type": "string", "description": "Image dimensions as 'WxH' (e.g. '1024x1024'); valid sizes depend on the backend. Defaults to '1024x1024'.", "default": "1024x1024"},
        },
        "required": ["prompt"],
    },
    execute=_generate_image_execute,
    intents=["general", "creative"],
    category="media",
    semantic_type="media",
    is_destructive=False,
    is_concurrency_safe=True,
)

edit_image_tool = build_tool(
    name="edit_image",
    description=(
        "Edit an existing image with a text prompt, optionally constrained to the "
        "area revealed by a mask. "
        "Use this to inpaint part of an image while preserving the rest, rather than "
        "regenerating from scratch. "
        "Do NOT use this for full-image regeneration (use generate_image) or for "
        "creative variations of the whole image (use image_variation). "
        "Tips: provide a mask where fully transparent pixels mark the editable "
        "region; describe the desired result, not the original. "
        "Pitfalls: the active backend must implement edit_image and accept the "
        "supplied image/mask encoding."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "image": {"type": "string", "description": "Source image as a base64-encoded string or a URL."},
            "prompt": {"type": "string", "description": "Description of the edit to apply."},
            "mask": {"type": "string", "description": "Mask image as a base64-encoded string or URL; transparent pixels indicate the region to edit."},
        },
        "required": ["image", "prompt"],
    },
    execute=_edit_image_execute,
    intents=["general", "creative"],
    category="media",
    semantic_type="media",
    is_destructive=False,
    is_concurrency_safe=True,
)

image_variation_tool = build_tool(
    name="image_variation",
    description=(
        "Generate one or more alternative variations of a source image, keeping the "
        "same subject but altering details. "
        "Use this to explore creative alternatives of an existing image without a "
        "text prompt; distinct from edit_image which targets a specific region. "
        "Do NOT use this for prompt-driven generation (use generate_image) or for "
        "masked edits (use edit_image). "
        "Tips: use a PNG source with a square aspect ratio for best compatibility; "
        "request multiple variations via `n`. "
        "Pitfalls: the active backend must implement create_image_variation - not "
        "all providers support this operation."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "image": {"type": "string", "description": "Source image as a base64-encoded string or a URL."},
            "n": {"type": "integer", "description": "Number of variations to generate; defaults to 1.", "default": 1},
            "size": {"type": "string", "description": "Output dimensions as 'WxH' (e.g. '1024x1024'); defaults to '1024x1024'.", "default": "1024x1024"},
        },
        "required": ["image"],
    },
    execute=_image_variation_execute,
    intents=["general", "creative"],
    category="media",
    semantic_type="media",
    is_destructive=False,
    is_concurrency_safe=True,
)

__all__ = ["generate_image_tool", "edit_image_tool", "image_variation_tool"]
