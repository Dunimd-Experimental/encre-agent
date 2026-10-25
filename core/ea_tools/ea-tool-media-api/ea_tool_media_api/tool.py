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

"""Media-API tools for the Encre tool system.

This module exposes four tools that wrap the active backend's
multimodal capabilities (Whisper-compatible transcription and
translation, plus embeddings and moderation):

- :data:`transcribe_audio_tool` -- speech-to-text via Whisper.
- :data:`translate_audio_tool` -- speech-to-English-text via Whisper.
- :data:`create_embeddings_tool` -- vector embeddings for text.
- :data:`create_moderation_tool` -- text moderation classification.

Each tool is a thin adapter over the backend: the module does not
implement the model call itself, it just defers to whichever
backend is currently attached to the agent loop. Every adapter
checks ``hasattr(backend, method)`` up front so an LLM that has
already decided to call the tool gets a friendly error message
rather than a cryptic AttributeError from the backend.

The :func:`_get_backend` helper is shared across all four adapters
and is deliberately not cached: re-resolving the loop on every call
is cheap, and it lets the caller swap backends between invocations
without restarting the process.
"""

import json
from typing import Any

from encre.tools.base import build_tool


def _get_backend():
    """Return the active backend, or ``None`` when no loop is attached.

    The import of :func:`_resolve_loop` is deferred to call time so
    that merely importing this module does not pull in the whole
    runtime module.
    """
    from encre.tools.runtime import _resolve_loop
    loop = _resolve_loop()
    if loop and loop.backend:
        return loop.backend
    return None


async def _transcribe_audio_execute(**kwargs: Any) -> str:
    """Run speech-to-text transcription on the active backend.

    Args:
        **kwargs: Tool arguments. ``file`` (required -- local path
            or base64 audio data), ``language`` (optional ISO code
            that hints the transcription).

    Returns:
        The transcribed text, or an ``Error:``-prefixed string on
        failure.
    """
    backend = _get_backend()
    if backend is None:
        return "Error: Backend is not initialized"
    if not hasattr(backend, "transcribe_audio"):
        return "Error: This backend does not support audio transcription"
    try:
        file = kwargs.get("file", "")
        language = kwargs.get("language", "")
        result = await backend.transcribe_audio(file=file, language=language or None)
        return result.text if hasattr(result, "text") else str(result)
    except Exception as e:
        return f"Error transcribing audio: {e}"


async def _translate_audio_execute(**kwargs: Any) -> str:
    """Run speech-to-English-text translation on the active backend.

    Args:
        **kwargs: Tool arguments. ``file`` (required -- local path
            or base64 audio data).

    Returns:
        The translated English text, or an ``Error:``-prefixed
        string on failure.
    """
    backend = _get_backend()
    if backend is None:
        return "Error: Backend is not initialized"
    if not hasattr(backend, "translate_audio"):
        return "Error: This backend does not support audio translation"
    try:
        file = kwargs.get("file", "")
        result = await backend.translate_audio(file=file)
        return result.text if hasattr(result, "text") else str(result)
    except Exception as e:
        return f"Error translating audio: {e}"


async def _create_embeddings_execute(**kwargs: Any) -> str:
    """Generate vector embeddings for text on the active backend.

    The response is truncated to the first 5 dimensions per item so
    the JSON stays small enough to include in a tool response; the
    full vectors are only reachable by calling the backend directly.
    This truncation is documented in the tool description so the
    agent does not assume the output is a usable embedding.

    Args:
        **kwargs: Tool arguments. ``input`` (required -- a string
            or a JSON-encoded list of strings for batch embedding).

    Returns:
        A JSON string with the model name, the number of embeddings
        generated, and a 3-item sample showing the first 5
        dimensions per embedding. Returns an ``Error:``-prefixed
        string on failure.
    """
    backend = _get_backend()
    if backend is None:
        return "Error: Backend is not initialized"
    if not hasattr(backend, "create_embeddings"):
        return "Error: This backend does not support embeddings"
    try:
        texts = kwargs.get("input", "")
        if isinstance(texts, str):
            texts = [texts]
        result = await backend.create_embeddings(input=texts)
        data = [{"index": i, "embedding": e.embedding[:5]} for i, e in enumerate(result.data)]
        return json.dumps({"model": result.model, "count": len(data), "sample": data[:3]}, indent=2)
    except Exception as e:
        return f"Error creating embeddings: {e}"


async def _create_moderation_execute(**kwargs: Any) -> str:
    """Classify text against a moderation policy on the active backend.

    Args:
        **kwargs: Tool arguments. ``input`` (required -- the text
            to classify).

    Returns:
        A JSON string with the ``flagged`` boolean list and the full
        per-result dict list, or an ``Error:``-prefixed string on
        failure.
    """
    backend = _get_backend()
    if backend is None:
        return "Error: Backend is not initialized"
    if not hasattr(backend, "create_moderation"):
        return "Error: This backend does not support moderation"
    try:
        text = kwargs.get("input", "")
        result = await backend.create_moderation(input=text)
        flagged = [r.flagged for r in result.results]
        return json.dumps({"flagged": flagged, "results": [r.to_dict() if hasattr(r, "to_dict") else r for r in result.results]}, indent=2, default=str)
    except Exception as e:
        return f"Error creating moderation: {e}"


transcribe_audio_tool = build_tool(
    name="transcribe_audio",
    description=(
        "Transcribe speech in an audio file into text using the active backend's "
        "transcription model (Whisper-compatible). "
        "Use this to convert recordings, voice notes, or interviews to text; prefer "
        "it over generic OCR for spoken content. "
        "Do NOT use this for in-browser live captioning or for translating audio to "
        "English (use translate_audio instead). "
        "Tips: pass an ISO language code to improve accuracy on non-English audio; "
        "use common formats such as mp3, wav, or m4a. "
        "Pitfalls: the active backend must implement transcribe_audio, otherwise the "
        "call returns an unsupported error."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "Local audio file path or base64-encoded audio data to transcribe."},
            "language": {"type": "string", "description": "ISO language code (e.g. 'en', 'zh', 'fr') to guide transcription; omit for auto-detection."},
        },
        "required": ["file"],
    },
    execute=_transcribe_audio_execute,
    intents=["general", "data"],
    category="media",
    semantic_type="media",
    is_concurrency_safe=True,
)

translate_audio_tool = build_tool(
    name="translate_audio",
    description=(
        "Translate speech in an audio file directly into English text using the "
        "active backend's translation model (Whisper-compatible). "
        "Use this when the source audio is non-English and English output is wanted "
        "in a single step; prefer transcribe_audio when you need the original-"
        "language text. "
        "Do NOT use this for same-language transcription or for translating written "
        "text (use translation). "
        "Tips: ensure the file format is supported by the backend; output is always "
        "English regardless of the source language. "
        "Pitfalls: the active backend must implement translate_audio, otherwise the "
        "call returns an unsupported error."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "Local audio file path or base64-encoded audio data to translate."},
        },
        "required": ["file"],
    },
    execute=_translate_audio_execute,
    intents=["general", "data"],
    category="media",
    semantic_type="media",
    is_concurrency_safe=True,
)

create_embeddings_tool = build_tool(
    name="create_embeddings",
    description=(
        "Generate vector embeddings for text inputs using the active backend's "
        "embeddings model. "
        "Use this to power semantic search, clustering, or similarity comparisons; "
        "returns a JSON sample of the first few embedding dimensions per input. "
        "Do NOT use this for chat completions (use the chat backend) or for "
        "moderation checks (use create_moderation). "
        "Tips: pass a single string or a JSON array of strings for batch embedding; "
        "keep inputs under the model's token limit. "
        "Pitfalls: output is truncated to 5 dimensions per item for display - "
        "retrieve full vectors from the backend directly if you need them."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Text to embed, or a JSON-encoded array of strings for batch embedding.",
            },
        },
        "required": ["input"],
    },
    execute=_create_embeddings_execute,
    intents=["general", "data"],
    category="data",
    is_concurrency_safe=True,
)

create_moderation_tool = build_tool(
    name="create_moderation",
    description=(
        "Classify text against a moderation policy to flag harmful or unsafe "
        "content, returning per-category results. "
        "Use this to screen user-generated or model-generated text before display, "
        "storage, or further processing. "
        "Do NOT use this for general sentiment analysis, PII detection, or as the "
        "sole safety gate without human review. "
        "Tips: submit plain text only; inspect the 'flagged' array and per-result "
        "category scores in the JSON output. "
        "Pitfalls: the active backend must implement create_moderation; policies and "
        "categories vary by provider."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "input": {"type": "string", "description": "Text to classify for harmful or unsafe content."},
        },
        "required": ["input"],
    },
    execute=_create_moderation_execute,
    intents=["general", "safety"],
    category="data",
    is_concurrency_safe=True,
)

__all__ = ["transcribe_audio_tool", "translate_audio_tool", "create_embeddings_tool", "create_moderation_tool"]
