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

# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "openai>=1.50.0",
#     "pillow>=10.0.0",
# ]
# ///

from __future__ import annotations

"""Generate and edit images using OpenAI's GPT Image 1.5 model via the Responses API.

This script is a standalone CLI tool (usable via ``uv run`` thanks to the PEP 723
inline dependency metadata block above) that wraps the OpenAI image generation and
inpainting capabilities. It supports three operation modes:

1. **Generation** — creates a new image from a text prompt using the Responses API
   (which orchestrates image generation through a tool-call pattern).
2. **Edit (full)** — applies a prompt-based edit to an entire input image by
   synthesising a fully transparent mask and calling the Image API.
3. **Edit (masked)** — applies a precise inpainting edit to the transparent regions
   of a user-supplied mask image.

Usage:
    # Generate a new image
    uv run generate_image.py --prompt "description" --filename "output.png" [options]

    # Edit an image (conversational, no mask)
    uv run generate_image.py --prompt "edit instructions" --filename "output.png" \\
        --input-image "input.png" [options]

    # Edit an image with a mask (precise inpainting)
    uv run generate_image.py --prompt "what to add" --filename "output.png" \\
        --input-image "input.png" --mask "mask.png" [options]
"""

import argparse
import base64
import os
import sys
from io import BytesIO
from pathlib import Path


def get_api_key(provided_key: str | None) -> str | None:
    """Resolve the OpenAI API key from CLI argument or environment variable.

    The CLI ``--api-key`` flag takes priority because it allows ephemeral or
    per-invocation keys without persisting them to the process environment. If
    neither source provides a key, ``None`` is returned so the caller can emit
    a clear error message.

    Args:
        provided_key: The API key string passed via ``--api-key``, or ``None``
            when the flag was omitted.

    Returns:
        A non-empty API key string, or ``None`` if no key was found.
    """
    if provided_key:
        return provided_key
    return os.environ.get("OPENAI_API_KEY")


def create_full_transparent_mask(image_path: str) -> bytes:
    """Create a fully transparent PNG mask matching the input image dimensions.

    When the user wants to edit an image but does not supply a mask, this
    function generates a mask where every pixel is fully transparent (alpha=0).
    In the OpenAI Image API, transparent regions in the mask indicate areas that
    should be inpainted, so a fully transparent mask effectively tells the API
    to regenerate the entire image according to the prompt.

    Args:
        image_path: Path to the input image; used only to read its dimensions
            so the mask has matching width and height.

    Returns:
        A PNG-encoded byte string representing the fully transparent mask.
    """
    from PIL import Image

    with Image.open(image_path) as img:
        width, height = img.size

    # All pixels have alpha=0, meaning the API will regenerate every pixel.
    mask = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    buf = BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


def generate_image_responses_api(
    client,
    prompt: str,
    quality: str = "medium",
    size: str = "1024x1024",
    background: str = "auto",
) -> bytes:
    """Generate a new image using the OpenAI Responses API with gpt-image-1.5.

    The Responses API uses a tool-call pattern: the model (gpt-4.1) receives the
    user's prompt along with an ``image_generation`` tool configuration and
    decides to invoke that tool. The response contains an ``image_generation_call``
    output item whose ``result`` field holds the base64-encoded image.

    Args:
        client: An ``OpenAI`` client instance with a valid API key.
        prompt: The text description of the image to generate.
        quality: Image quality tier — ``"low"``, ``"medium"`` (default), or
            ``"high"``. Higher quality produces better results at the cost of
            slower generation and higher token consumption.
        size: Output dimensions. Accepts ``"1024x1024"``, ``"1024x1536"``,
            ``"1536x1024"``, or ``"auto"`` (lets the API choose).
        background: Background type — ``"transparent"``, ``"opaque"``, or
            ``"auto"``. ``"auto"`` lets the model decide.

    Returns:
        The decoded image bytes.

    Raises:
        RuntimeError: If the response contains no image generation call, which
            typically means the prompt was too vague or the API returned a text
            response instead of invoking the image tool.
    """
    # Build the tool configuration that tells the model how to generate images.
    tool_config = {
        "type": "image_generation",
        "quality": quality,
    }

    # Only set size/background when not left on "auto" — the API treats
    # explicit values differently from the default and may reject "auto" for
    # certain size combinations.
    if size != "auto":
        tool_config["size"] = size

    if background != "auto":
        tool_config["background"] = background

    # gpt-4.1 is the orchestrator model; it interprets the prompt and invokes
    # the image_generation tool. The actual pixel rendering is handled by the
    # gpt-image-1.5 backend under the hood.
    response = client.responses.create(
        model="gpt-4.1",
        input=prompt,
        tools=[tool_config],
    )

    # The response.output is a list of output items. We iterate to find the
    # one that actually contains the generated image bytes.
    for output in response.output:
        if output.type == "image_generation_call":
            return base64.b64decode(output.result)

    raise RuntimeError("No image was generated in the response")


def edit_image_with_mask(
    client,
    prompt: str,
    image_path: str,
    mask_path: str | None,
    size: str = "1024x1024",
) -> bytes:
    """Edit an image using the OpenAI Image API with optional mask inpainting.

    This function handles both full-image edits (when ``mask_path`` is ``None``)
    and precise inpainting (when a mask is supplied). When no mask is given, a
    fully transparent mask is synthesised so the API treats the entire image as
    an inpainting region.

    The Image API requires both an image and a mask as file-like objects. When
    a user-supplied mask path is provided, the file is opened for reading; when
    synthesised, a ``BytesIO`` buffer is used with a virtual filename so the
    API can inspect the extension.

    Args:
        client: An ``OpenAI`` client instance with a valid API key.
        prompt: Text describing the edit or content to add in masked regions.
        image_path: Path to the source image to edit.
        mask_path: Path to a PNG mask where transparent (alpha=0) regions indicate
            areas to inpaint. If ``None``, a fully transparent mask is generated
            automatically to allow editing the entire image.
        size: Output dimensions. When ``"auto"`` is passed, ``"1024x1024"`` is
            used as the fallback because the Image API does not accept ``"auto"``
            for this endpoint.

    Returns:
        The decoded edited image bytes.
    """
    from PIL import Image

    # Choose the mask source: user-supplied file or synthesised transparent mask.
    # The BytesIO object needs a ``name`` attribute set to ``"mask.png"`` so
    # the OpenAI SDK can infer the media type from the filename extension.
    if mask_path:
        mask_file = open(mask_path, "rb")
    else:
        mask_bytes = create_full_transparent_mask(image_path)
        mask_file = BytesIO(mask_bytes)
        mask_file.name = "mask.png"

    try:
        # size="auto" is not accepted by the Image API's edit endpoint; fall
        # back to the default 1024x1024 resolution.
        result = client.images.edit(
            model="gpt-image-1.5",
            image=open(image_path, "rb"),
            mask=mask_file,
            prompt=prompt,
            size=size if size != "auto" else "1024x1024",
        )

        image_base64 = result.data[0].b64_json
        return base64.b64decode(image_base64)
    finally:
        # Ensure the user-supplied mask file handle is released even if the
        # API call raised an exception.
        if mask_path:
            mask_file.close()


def main():
    """CLI entry point: parse arguments, call the OpenAI API, and save the result.

    The main function orchestrates the full pipeline:
    1. Parse command-line arguments with argparse.
    2. Resolve the API key and exit early if none is available (avoids importing
       the heavy ``openai`` library when the script will fail anyway).
    3. Determine the operation mode based on the presence of ``--input-image``
       and ``--mask`` flags.
    4. Call the appropriate API function and capture the resulting image bytes.
    5. Post-process the image (format conversion, transparency handling) and save
       it to the output path.
    """
    parser = argparse.ArgumentParser(
        description="Generate and edit images using OpenAI GPT Image 1.5"
    )
    parser.add_argument(
        "--prompt", "-p",
        required=True,
        help="Image description or editing instructions",
    )
    parser.add_argument(
        "--filename", "-f",
        required=True,
        help="Output filename (e.g., output.png)",
    )
    parser.add_argument(
        "--input-image", "-i",
        help="Optional input image path for editing",
    )
    parser.add_argument(
        "--mask", "-m",
        help="Optional mask image path for precise inpainting (PNG with transparent areas to edit)",
    )
    parser.add_argument(
        "--quality", "-q",
        choices=["low", "medium", "high"],
        default="medium",
        help="Output quality: low, medium (default), or high",
    )
    parser.add_argument(
        "--size", "-s",
        choices=["1024x1024", "1024x1536", "1536x1024", "auto"],
        default="1024x1024",
        help="Output size (default: 1024x1024)",
    )
    parser.add_argument(
        "--background", "-b",
        choices=["transparent", "opaque", "auto"],
        default="auto",
        help="Background type for generation (default: auto)",
    )
    parser.add_argument(
        "--api-key", "-k",
        help="OpenAI API key (overrides OPENAI_API_KEY env var)",
    )

    args = parser.parse_args()

    # Resolve the API key before importing OpenAI to fail fast with a clear
    # message rather than a confusing import error or connection timeout.
    api_key = get_api_key(args.api_key)
    if not api_key:
        print("Error: No API key provided.", file=sys.stderr)
        print("Please either:", file=sys.stderr)
        print("  1. Provide --api-key argument", file=sys.stderr)
        print("  2. Set OPENAI_API_KEY environment variable", file=sys.stderr)
        sys.exit(1)

    # Import here (after API key validation) to keep the module import time
    # short when the script will fail early.
    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    # Ensure the output directory exists (e.g., if the user specified
    # "output/images/myimage.png" and "output/images/" doesn't exist).
    output_path = Path(args.filename)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine operation mode based on which flags were supplied.
    if args.input_image:
        # Validate the input image exists before making an API call.
        if not Path(args.input_image).exists():
            print(f"Error: Input image not found: {args.input_image}", file=sys.stderr)
            sys.exit(1)

        if args.mask:
            # Validate the mask exists before making an API call.
            if not Path(args.mask).exists():
                print(f"Error: Mask image not found: {args.mask}", file=sys.stderr)
                sys.exit(1)

            print("Editing image with mask using Image API...")
            print(f"  Input: {args.input_image}")
            print(f"  Mask: {args.mask}")
            print(f"  Size: {args.size}")

            try:
                image_bytes = edit_image_with_mask(
                    client,
                    args.prompt,
                    args.input_image,
                    args.mask,
                    args.size,
                )
            except Exception as e:
                print(f"Error editing image with mask: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            # Full-image edit: the API will regenerate the entire image
            # according to the prompt. The auto-generated transparent mask
            # marks every pixel as an inpainting target.
            print("Editing image using Image API (full image edit)...")
            print(f"  Input: {args.input_image}")
            print(f"  Size: {args.size}")

            try:
                image_bytes = edit_image_with_mask(
                    client,
                    args.prompt,
                    args.input_image,
                    None,  # No mask — a full transparent mask is generated internally
                    args.size,
                )
            except Exception as e:
                print(f"Error editing image: {e}", file=sys.stderr)
                sys.exit(1)
    else:
        # Generation mode: create a brand-new image from the text prompt.
        print("Generating image using Responses API...")
        print(f"  Quality: {args.quality}")
        print(f"  Size: {args.size}")
        print(f"  Background: {args.background}")

        try:
            image_bytes = generate_image_responses_api(
                client,
                args.prompt,
                args.quality,
                args.size,
                args.background,
            )
        except Exception as e:
            print(f"Error generating image: {e}", file=sys.stderr)
            sys.exit(1)

    # --- Post-processing and saving -------------------------------------------
    from PIL import Image

    image = Image.open(BytesIO(image_bytes))

    # Decide whether to preserve alpha transparency based on the user's
    # background preference or the requested output format.
    if args.background == "transparent" or output_path.suffix.lower() == ".png":
        # Keep RGBA so transparency is preserved in the output PNG.
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        image.save(str(output_path), "PNG")
    else:
        # For opaque output, flatten any alpha channel onto a white background
        # before saving. This prevents unexpected transparency when the user
        # requested an opaque background or a non-PNG format.
        if image.mode == "RGBA":
            rgb_image = Image.new("RGB", image.size, (255, 255, 255))
            rgb_image.paste(image, mask=image.split()[3])
            rgb_image.save(str(output_path), "PNG")
        elif image.mode == "RGB":
            image.save(str(output_path), "PNG")
        else:
            image.convert("RGB").save(str(output_path), "PNG")

    full_path = output_path.resolve()
    print(f"\nImage saved: {full_path}")


if __name__ == "__main__":
    main()
