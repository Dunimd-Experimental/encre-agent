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

"""Screen OCR -- extract visible text with bounding boxes from the desktop.

One backend per platform, imported directly (dependencies are declared
in the package dependency list, not guessed at import time):

* Windows.Media.Ocr on Windows 10+ (no external dependencies).
* Vision.framework on macOS 10.15+ (pyobjc-framework-Vision).
* pytesseract elsewhere (requires the Tesseract binary).
"""

import asyncio
import concurrent.futures
import io
import logging
import sys
from typing import Any

logger = logging.getLogger("encre.computer.ocr")


def _ocr_winrt(image_bytes: bytes) -> list[dict[str, Any]] | None:
    """Windows OCR via winrt-Windows.Media.Ocr (Win 10+ built-in)."""
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import (
        DataWriter,
        InMemoryRandomAccessStream,
    )

    async def _run() -> list[dict[str, Any]]:
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            logger.warning("Windows OCR: no OCR language packs installed")
            return []

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(image_bytes)
        try:
            await writer.store_async()
        finally:
            writer.detach_stream()
        stream.seek(0)

        decoder = await BitmapDecoder.create_async(stream)
        # No-arg overload only in winrt 3.x (the format+alphaMode overload
        # is not projected); the decoder returns the source-native format.
        software_bitmap = await decoder.get_software_bitmap_async()

        result = await engine.recognize_async(software_bitmap)

        lines = []
        for line in result.lines:
            # winrt 3.x: OcrLine exposes only ``text`` + ``words`` -- merge
            # the per-word rects into one line-level bounding box.
            x0 = y0 = x1 = y1 = None
            for word in line.words:
                wr = word.bounding_rect
                wx0, wy0 = float(wr.x), float(wr.y)
                wx1, wy1 = wx0 + float(wr.width), wy0 + float(wr.height)
                if x0 is None:
                    x0, y0, x1, y1 = wx0, wy0, wx1, wy1
                else:
                    x0, y0 = min(x0, wx0), min(y0, wy0)
                    x1, y1 = max(x1, wx1), max(y1, wy1)
            if x0 is None:
                continue
            w = x1 - x0
            h = y1 - y0
            lines.append(
                {
                    "text": line.text,
                    "x": int(x0),
                    "y": int(y0),
                    "width": int(w),
                    "height": int(h),
                    "center_x": int(x0 + w / 2),
                    "center_y": int(y0 + h / 2),
                }
            )
        return lines

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop here (sync caller) -- own a private loop.
        try:
            return asyncio.run(_run())
        except Exception as e:
            logger.warning("Windows OCR failed: %s", e)
            return None
    # Already inside a running loop: blocking it with
    # ``run_coroutine_threadsafe(...).result()`` would deadlock until the
    # timeout (the loop can never service the coroutine it is blocked on).
    # Run the coroutine on a fresh loop inside a worker thread instead.
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _run()).result(timeout=30)
    except Exception as e:
        logger.warning("Windows OCR failed: %s", e)
        return None


def _ocr_macos_vision(image_bytes: bytes) -> list[dict[str, Any]] | None:
    """macOS OCR via Vision.framework (macOS 10.15+ built-in).

    Requires ``pyobjc-framework-Vision``.  Install with::

        pip install pyobjc-framework-Vision

    When available this gives native-quality text recognition with no
    separate binary dependency (no Tesseract needed on macOS).
    """
    import Vision  # type: ignore
    from Foundation import NSData  # type: ignore

    try:
        ns_data = NSData.dataWithBytes_length_(image_bytes, len(image_bytes))
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(False)

        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
            ns_data,
            None,
        )
        success, error = handler.performRequests_error_([request], None)
        if not success or error is not None:
            return None

        # Denormalise Vision coordinates (0.0-1.0, bottom-left origin)
        # into pixel coordinates (top-left origin, matching our desktop
        # tool coordinate system).
        from PIL import Image

        pil_img = Image.open(io.BytesIO(image_bytes))
        img_width, img_height = pil_img.size

        lines = []
        for observation in request.results():
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            text = candidates[0].string()
            if not text.strip():
                continue

            box = observation.boundingBox()
            x = box.origin.x * img_width
            y = (1.0 - box.origin.y - box.size.height) * img_height
            w = box.size.width * img_width
            h = box.size.height * img_height

            lines.append(
                {
                    "text": text.strip(),
                    "x": int(x),
                    "y": int(y),
                    "width": int(w),
                    "height": int(h),
                    "center_x": int(x + w / 2),
                    "center_y": int(y + h / 2),
                }
            )
        return lines
    except Exception as e:
        logger.warning("macOS Vision OCR failed: %s", e)
        return None


def _ocr_pytesseract(image_bytes: bytes) -> list[dict[str, Any]] | None:
    """Fallback OCR via pytesseract (requires Tesseract installed)."""
    import pytesseract
    from PIL import Image

    try:
        pil_img = Image.open(io.BytesIO(image_bytes))
        data = pytesseract.image_to_data(pil_img, output_type=pytesseract.Output.DICT)
        lines = []
        seen = set()
        for i in range(len(data["text"])):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            # Deduplicate near-identical boxes (pytesseract often emits word fragments)
            key = (x // 5, y // 5, text.lower())
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                {
                    "text": text,
                    "x": x,
                    "y": y,
                    "width": w,
                    "height": h,
                    "center_x": x + w // 2,
                    "center_y": y + h // 2,
                }
            )
        return lines
    except Exception as e:
        logger.warning("pytesseract OCR failed: %s", e)
        return None


def ocr_image(image_bytes: bytes) -> list[dict[str, Any]]:
    """Extract visible text + bounding boxes from a PNG screenshot.

    Dispatches on the running platform -- exactly one backend per OS,
    each imported directly (its dependency is declared in this
    package's dependency list, so pip guarantees the import):

      * Windows: Windows.Media.Ocr (WinRT, Win 10+ built-in)
      * macOS:   Vision.framework (macOS 10.15+)
      * else:    pytesseract (requires the Tesseract binary)

    Returns a list of dicts::

        [
            {
                "text": "Login",
                "x": 450, "y": 300,
                "width": 100, "height": 40,
                "center_x": 500, "center_y": 320,
            },
            ...
        ]
    """
    if sys.platform == "win32":
        result = _ocr_winrt(image_bytes)
    elif sys.platform == "darwin":
        result = _ocr_macos_vision(image_bytes)
    else:
        result = _ocr_pytesseract(image_bytes)
    return result or []


__all__ = ["ocr_image"]
