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

from encre.logging_config import get_logger

"""
Multimodal capability mixin for OpenAI-protocol backends.

The :class:`MultimodalMixin` class implements the full surface of the
OpenAI platform API (images, audio, embeddings, moderation, files, batch,
fine-tuning, realtime, responses) on top of the same ``httpx.AsyncClient``
already used by :class:`encre.backends.openai_sse.OpenAISSEBackend`.

Because the mixin only depends on:

* :attr:`self.api_base_url`  -- the API root (no trailing slash).
* :attr:`self.api_key`        -- the bearer token.
* :attr:`self.model`          -- default model.
* :attr:`self._get_client()`  -- the shared :class:`httpx.AsyncClient`.
* :attr:`self._build_headers()` -- request headers (with auth).

any subclass that provides these (all OpenAI-protocol backends, plus the
:class:`OpenAISSEBackend`-based DeepSeek / Groq / Alibaba / GLM / Kimi /
Tencent / Volcengine / Xiaomi / OpenRouter / HuggingFace / ... families)
gains multimodal coverage automatically.

Provider-specific overrides:

* :meth:`MultimodalMixin._normalize_image_url` -- strip trailing
  ``/chat/completions`` from a base URL that was accidentally configured.
* :meth:`MultimodalMixin._coerce_realtime_transport` -- map the optional
  ``httpx_ws`` package to a transport instance for Realtime.

The mixin is additive; every multimodal method starts by checking the
    relevant ``supports_*`` flag and falls back to :class:`NotImplementedError`
    when the feature is not available.
    """

import asyncio
import base64
import inspect
import json
import logging
import re
import time
from typing import Any

import httpx

from encre.utils.types import (
    AudioResult,
    BatchListResponse,
    BatchObject,
    BatchRequest,
    EmbeddingResponse,
    EmbeddingResult,
    ProviderFileContent,
    FileListResponse,
    FileObject,
    FineTuneEvent,
    FineTuneHyperparameters,
    FineTuneJob,
    FineTuneJobList,
    ImageGenerationResponse,
    ImageResult,
    ModerationCategory,
    ModerationResponse,
    ModerationResult,
    RealtimeSession,
    RealtimeSessionConfig,
    ResponseObject,
)

# Central logger for all multimodal API activity.
_LOG = logging.getLogger("encre.backend.multimodal")


def _b64_to_bytes(value: str) -> bytes:
    """Decode a base64 string.  Falls back gracefully on invalid padding."""
    try:
        return base64.b64decode(value)
    except Exception:
        # Some providers strip padding; add it back.
        padded = value + "=" * (-len(value) % 4)
        return base64.b64decode(padded)


def _bytes_to_b64(data: bytes) -> str:
    """Encode bytes to a base64 string with no newlines."""
    return base64.b64encode(data).decode("ascii")


# ── Multimodal capability probing ──────────────────────────────────

# Tiny valid 1x1 transparent PNG injected into a probe message to test
# whether the provider endpoint accepts image (multimodal) content.
_PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# Probe failures are classified: only definitive negative signals (HTTP
# 404/405, connection refused) prove an endpoint is absent and are recorded
# as "unsupported".  Transient failures (429, timeouts, auth errors, 5xx)
# are inconclusive and recorded as "unknown" so they are never cached as a
# permanent "unsupported" verdict.

# Probe budget.  Validation runs inside a settings dialog that holds one
# WebSocket round trip open, so every probe step has an explicit ceiling:
# an unresponsive endpoint must time out fast instead of stalling the whole
# validation (the previous behaviour let one hanging "hi" request consume
# the client's entire wait window).
_PROBE_NODE_TIMEOUT = 4.0       # single HTTP probe node
_PROBE_CHAT_TIMEOUT = 12.0     # one chat completion (connectivity / level)
_PROBE_REASONING_TIMEOUT = 35.0  # all reasoning levels, inclusive
_PROBE_TOTAL_TIMEOUT = 20.0    # whole capability probe


class _ProbeTimeoutError(Exception):
    """Raised when the connectivity probe does not answer in time."""


def _probe_wav_bytes() -> bytes:
    """Build a tiny valid mono PCM WAV used to probe audio endpoints."""
    import struct
    samples = b"\x00\x00" * 320
    fmt_chunk = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
    data_chunk = b"data" + struct.pack("<I", len(samples)) + samples
    return b"RIFF" + struct.pack("<I", 4 + len(fmt_chunk) + len(data_chunk)) + b"WAVE" + fmt_chunk + data_chunk


def _anthropic_headers(backend: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    auth_manager = getattr(backend, "auth_manager", None)
    if auth_manager is not None and getattr(auth_manager, "api_key", ""):
        headers["x-api-key"] = auth_manager.api_key
    elif getattr(backend, "api_key", ""):
        headers["x-api-key"] = backend.api_key
    return headers


async def probe_multimodal_support(backend: Any) -> str:
    """Probe whether a backend endpoint accepts image (multimodal) content.

    Sends a minimal chat message carrying a tiny 1x1 PNG.  Returns:

    * ``"supported"``   -- the endpoint accepted the image block.
    * ``"unsupported"`` -- a definitive negative: the provider rejected the
      image content or the endpoint is not reachable (connection refused).
    * ``"unknown"``     -- an inconclusive probe (429, timeout, auth error,
      5xx).  The caller must not treat this as a permanent negative.

    OpenAI-protocol backends are probed with an ``image_url`` content
    block; Anthropic-protocol backends with a native ``image`` block.

    When a backend *declares* that it cannot take vision input
    (``supports_vision_input() -> False``), that is treated as authoritative
    -- but only on the provider's official endpoint (``DEFAULT_BASE_URL``).
    Many text-only endpoints (e.g. DeepSeek) silently accept an
    ``image_url`` content block and still return 200 -- the declared flag is
    the only reliable signal for them.  A custom ``base_url`` (relay,
    gateway, self-hosted server) may route the same backend type to a
    vision-capable model, so there the declared negative is ignored and the
    live probe decides.
    """
    declared = getattr(backend, "supports_vision_input", None)
    if callable(declared):
        try:
            if not declared():
                # A declared negative is only authoritative on the provider's
                # official endpoint.  A custom base_url (relay, gateway,
                # self-hosted server) may route the same backend type to a
                # vision-capable model, so fall through to the live probe.
                endpoint = str(
                    getattr(backend, "api_base_url", None)
                    or getattr(backend, "base_url", "")
                    or ""
                ).rstrip("/")
                default_base = str(
                    getattr(backend, "DEFAULT_BASE_URL", "") or ""
                ).rstrip("/")
                if endpoint == default_base:
                    return "unsupported"
        except Exception:
            logger.debug("exception suppressed", exc_info=True)
    if hasattr(backend, "_post_json_async") and hasattr(backend, "_multimodal_base"):
        # OpenAI / OpenAI-compatible protocol.
        return await _probe_openai_protocol(backend)
    try:
        from encre.backends.anthropic import AnthropicBackend
        if isinstance(backend, AnthropicBackend):
            return await _probe_anthropic_protocol(backend)
    except Exception:
        logger.debug("exception suppressed", exc_info=True)
    try:
        from encre.backends.google import GoogleBackend
        if isinstance(backend, GoogleBackend):
            return await _probe_google_protocol(backend)
    except Exception:
        logger.debug("exception suppressed", exc_info=True)
    # Final fallback: probe through the backend's own chat() method so
    # method-level backends (Bedrock, Local, Failover, Router, ...) are still
    # verified for multimodal input.
    return await _probe_common_protocol(backend)


async def _probe_openai_protocol(backend: Any) -> str:
    payload: dict[str, Any] = {
        "model": backend.model,
        "max_tokens": 8,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is in this image?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{_PROBE_PNG_B64}"
                        },
                    },
                ],
            }
        ],
    }
    try:
        await backend._post_json_async("/chat/completions", payload)
        return "supported"
    except Exception as exc:
        return _probe_error_verdict(exc)


async def _probe_anthropic_protocol(backend: Any) -> str:
    body: dict[str, Any] = {
        "model": backend.model,
        "max_tokens": 8,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": _PROBE_PNG_B64,
                        },
                    },
                    {"type": "text", "text": "what is in this image?"},
                ],
            }
        ],
    }
    try:
        resp = await backend._client.post("/messages", json=body, headers=_anthropic_headers(backend))
        if resp.status_code < 400:
            return "supported"
        return _status_verdict(resp.status_code)
    except Exception as exc:
        return _probe_error_verdict(exc)


async def _probe_google_protocol(backend: Any) -> str:
    """Probe Google / Gemini multimodal input with a native 1x1 PNG inline_data part."""
    model = getattr(backend, "model", "gemini-2.5-pro")
    base_url = str(getattr(backend, "base_url", "")).rstrip("/")
    url = f"{base_url}/models/{model}:generateContent"
    body: dict[str, Any] = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/png", "data": _PROBE_PNG_B64}},
            {"text": "what is in this image?"},
        ]}],
    }
    try:
        resp = await backend._client.post(
            url, params={"key": getattr(backend, "api_key", "")}, json=body
        )
        if resp.status_code < 400:
            return "supported"
        return _status_verdict(resp.status_code)
    except Exception as exc:
        return _probe_error_verdict(exc)


# ── Full node / endpoint probing ─────────────────────────────────────

# Canonical node names recorded per model in ``ModelConfig.capabilities``.
_NODE_CHAT = "chat"
_NODE_RESPONSES = "responses"
_NODE_IMAGES_GENERATION = "images_generation"
_NODE_IMAGES_EDIT = "images_edit"
_NODE_IMAGES_VARIATION = "images_variation"
_NODE_AUDIO_TRANSLATION = "audio_translation"
_NODE_EMBEDDINGS = "embeddings"
_NODE_MODERATION = "moderation"
_NODE_FILES = "files"
_NODE_BATCHES = "batches"
_NODE_FINE_TUNING = "fine_tuning"
_NODE_REALTIME = "realtime"
_NODE_MODELS = "models"
_NODE_MULTIMODAL_INPUT = "multimodal_input"

# All probed nodes (multimodal_input is added only when multimodal is enabled).
_ALL_NODES = (
    _NODE_CHAT,
    _NODE_RESPONSES,
    _NODE_IMAGES_GENERATION,
    _NODE_IMAGES_EDIT,
    _NODE_IMAGES_VARIATION,
    _NODE_AUDIO_TRANSLATION,
    _NODE_EMBEDDINGS,
    _NODE_MODERATION,
    _NODE_FILES,
    _NODE_BATCHES,
    _NODE_FINE_TUNING,
    _NODE_REALTIME,
    _NODE_MODELS,
)


def _status_verdict(status: int) -> str:
    """Map an HTTP status from a probe request to a probe verdict."""
    return "unsupported" if status in (404, 405) else "unknown"


def _probe_error_verdict(exc: BaseException) -> str:
    """Classify a probe failure as ``"unsupported"`` or ``"unknown"``.

    Only definitive negative signals prove an endpoint is absent:

    * HTTP 404 / 405 (endpoint does not exist / method not allowed);
    * connection refused / unreachable host (``httpx.ConnectError``,
      ``httpx.ConnectTimeout``).

    Everything else (429 rate limits, timeouts, auth errors, 5xx, provider
    quirks) is inconclusive and reported as ``"unknown"`` so a transient
    outage is never cached as a permanent ``"unsupported"`` capability.
    """
    status: int | None = None
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    else:
        match = re.search(r"HTTP (\d{3})", str(exc))
        if match:
            status = int(match.group(1))
    if status is not None:
        return _status_verdict(status)
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "unsupported"
    return "unknown"


async def _node_supported(coro: Any) -> str:
    """Await a probe request and return the node verdict.

    Each node is capped at :data:`_PROBE_NODE_TIMEOUT`: a slow or hung
    endpoint must degrade to ``"unknown"`` instead of stalling the whole
    capability probe.  Returns ``"supported"`` when the request succeeded,
    ``"unsupported"`` for a definitive negative (404/405, connection
    refused) and ``"unknown"`` for transient failures (see
    :func:`_probe_error_verdict`).
    """
    try:
        await asyncio.wait_for(coro, timeout=_PROBE_NODE_TIMEOUT)
        return "supported"
    except Exception as exc:
        return _probe_error_verdict(exc)


async def _validate_chat(backend: Any, max_tokens: int) -> None:
    """Fire the minimal connectivity chat used by model validation.

    Consumes the whole (non-streaming) response so a backend that resolves
    lazily still raises its errors here.  Raises when the backend reports
    an error event.
    """
    events = backend.chat(
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=max_tokens,
        stream=False,
    )
    if inspect.isasyncgen(events):
        events = [ev async for ev in events]
    elif inspect.isawaitable(events):
        events = await events
    if inspect.isasyncgen(events):
        events = [ev async for ev in events]
    for ev in events or []:
        err = getattr(ev, "error", None)
        if err:
            raise err if isinstance(err, Exception) else RuntimeError(str(err))


async def probe_backend_capabilities(
    backend: Any, include_multimodal: bool = False
) -> dict[str, str]:
    """Probe every node/endpoint the backend protocol exposes.

    Returns ``{node_name: "supported" | "unsupported" | "unknown"}``.  Runs
    entirely in the background (never blocks model configuration).  Only
    definitive negatives (404/405, connection refused) are recorded as
    ``"unsupported"``; transient failures are recorded as ``"unknown"``.
    The multimodal input probe only runs when ``include_multimodal`` is
    set -- i.e. the multimodal option was enabled for this model.
    """
    if hasattr(backend, "_post_json_async") and hasattr(backend, "_multimodal_base"):
        # OpenAI / OpenAI-compatible protocol (covers most backends).
        return await _probe_openai_nodes(backend, include_multimodal)
    try:
        from encre.backends.anthropic import AnthropicBackend
        if isinstance(backend, AnthropicBackend):
            return await _probe_anthropic_nodes(backend, include_multimodal)
    except Exception:
        logger.debug("exception suppressed", exc_info=True)
    try:
        from encre.backends.google import GoogleBackend
        if isinstance(backend, GoogleBackend):
            return await _probe_google_nodes(backend, include_multimodal)
    except Exception:
        logger.debug("exception suppressed", exc_info=True)
    # Bedrock / Local / Failover / Router / anything else: probe through the
    # backend's own methods (the SDK / in-process API is the node surface).
    return await _probe_common_nodes(backend, include_multimodal)


async def _probe_openai_nodes(backend: Any, include_multimodal: bool) -> dict[str, str]:
    out: dict[str, str] = {name: "unsupported" for name in _ALL_NODES}
    png = base64.b64decode(_PROBE_PNG_B64)

    def _may(flag: str) -> bool:
        method = getattr(backend, flag, None)
        if method is None:
            return True
        try:
            return bool(method())
        except Exception:
            return True

    out[_NODE_CHAT] = await _node_supported(backend._post_json_async("/chat/completions", {
            "model": backend.model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8}))
    out[_NODE_MODELS] = await _node_supported(backend._get_json_async("/models"))
    if _may("supports_responses_api"):
        out[_NODE_RESPONSES] = await _node_supported(
            backend._post_json_async("/responses", {
                "model": backend.model, "input": "hi", "max_output_tokens": 8}))
    if _may("supports_image_generation"):
        out[_NODE_IMAGES_GENERATION] = await _node_supported(
            backend._post_json_async("/images/generations", {
                "model": backend.model, "prompt": "a tiny test", "n": 1, "size": "256x256"}))
    if _may("supports_image_edit"):
        out[_NODE_IMAGES_EDIT] = await _node_supported(
            backend._post_multipart_async("/images/edits",
                {"model": backend.model, "prompt": "test", "size": "256x256"},
                {"image": ("probe.png", png, "image/png")}))
    if _may("supports_image_variation"):
        out[_NODE_IMAGES_VARIATION] = await _node_supported(
            backend._post_multipart_async("/images/variations",
                {"model": backend.model, "n": 1, "size": "256x256"},
                {"image": ("probe.png", png, "image/png")}))
    out[_NODE_AUDIO_TRANSLATION] = await _node_supported(
        backend._post_multipart_async("/audio/translations",
            {"model": backend.model},
            {"file": ("probe.wav", _probe_wav_bytes(), "audio/wav")}))
    if _may("supports_embeddings"):
        out[_NODE_EMBEDDINGS] = await _node_supported(
            backend._post_json_async("/embeddings", {
                "model": backend.model, "input": "hi"}))
    if _may("supports_moderation"):
        out[_NODE_MODERATION] = await _node_supported(
            backend._post_json_async("/moderations", {"input": "hi"}))

    async def _files_probe() -> None:
        data = await backend._post_multipart_async(
            "/files",
            {"purpose": "assistants"},
            {"file": ("probe.txt", b"encre capability probe", "text/plain")},
        )
        file_id = (data or {}).get("id")
        if file_id:
            await backend._delete_async(f"/files/{file_id}")

    if _may("supports_files"):
        out[_NODE_FILES] = await _node_supported(_files_probe())
    if _may("supports_batch"):
        out[_NODE_BATCHES] = await _node_supported(backend._get_json_async("/batches"))
    if _may("supports_fine_tuning"):
        out[_NODE_FINE_TUNING] = await _node_supported(backend._get_json_async("/fine_tuning/jobs"))
    if _may("supports_realtime"):
        out[_NODE_REALTIME] = await _node_supported(
            backend._post_json_async("/realtime/sessions", {"model": backend.model}))

    if include_multimodal:
        out[_NODE_MULTIMODAL_INPUT] = await probe_multimodal_support(backend)
    return out


async def _probe_anthropic_nodes(backend: Any, include_multimodal: bool) -> dict[str, str]:
    out: dict[str, str] = {name: "unsupported" for name in _ALL_NODES}
    client = getattr(backend, "_client", None)
    if client is None:
        if include_multimodal:
            out[_NODE_MULTIMODAL_INPUT] = await probe_multimodal_support(backend)
        return out
    headers = _anthropic_headers(backend)

    def _may(flag: str) -> bool:
        method = getattr(backend, flag, None)
        if method is None:
            return True
        try:
            return bool(method())
        except Exception:
            return True

    async def _post(path: str, body: dict[str, Any]) -> None:
        resp = await client.post(path, json=body, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")

    async def _get(path: str) -> None:
        resp = await client.get(path, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")

    out[_NODE_CHAT] = await _node_supported(_post("/messages", {
            "model": backend.model, "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]}))
    out[_NODE_MODELS] = await _node_supported(_get("/models"))
    if _may("supports_files"):
        out[_NODE_FILES] = await _node_supported(_get("/files"))
    if _may("supports_batch"):
        out[_NODE_BATCHES] = await _node_supported(_get("/messages/batches"))

    # The OpenAI-style nodes are probed against the Anthropic host too: a
    # definitive negative (404/405) confirms the endpoint is simply not
    # offered, while other failures are recorded as "unknown".
    if _may("supports_responses_api"):
        out[_NODE_RESPONSES] = await _node_supported(_post("/responses", {"model": backend.model, "input": "hi", "max_output_tokens": 8}))
    if _may("supports_image_generation"):
        out[_NODE_IMAGES_GENERATION] = await _node_supported(_post("/images/generations", {"model": backend.model, "prompt": "test", "n": 1, "size": "256x256"}))
    if _may("supports_image_edit"):
        out[_NODE_IMAGES_EDIT] = await _node_supported(_post("/images/edits", {"model": backend.model, "prompt": "test"}))
    if _may("supports_image_variation"):
        out[_NODE_IMAGES_VARIATION] = await _node_supported(_post("/images/variations", {"model": backend.model, "n": 1}))
    out[_NODE_AUDIO_TRANSLATION] = await _node_supported(_post("/audio/translations", {"model": backend.model}))
    if _may("supports_embeddings"):
        out[_NODE_EMBEDDINGS] = await _node_supported(_post("/embeddings", {"model": backend.model, "input": "hi"}))
    if _may("supports_moderation"):
        out[_NODE_MODERATION] = await _node_supported(_post("/moderations", {"input": "hi"}))
    if _may("supports_fine_tuning"):
        out[_NODE_FINE_TUNING] = await _node_supported(_get("/fine_tuning/jobs"))
    if _may("supports_realtime"):
        out[_NODE_REALTIME] = await _node_supported(_post("/realtime/sessions", {"model": backend.model}))

    if include_multimodal:
        out[_NODE_MULTIMODAL_INPUT] = await probe_multimodal_support(backend)
    return out


async def _probe_google_nodes(backend: Any, include_multimodal: bool) -> dict[str, str]:
    """Probe Google / Gemini Generative Language API nodes.

    Follows the exact endpoints :class:`GoogleBackend` uses (chat via
    ``:generateContent``, read-only checks for models / files / tunedModels,
    and the batch / embedding nodes).  Every failure is recorded as
    ``unsupported``.  Nodes Google does not offer through this API
    (image generation, audio, moderation, responses, realtime, ...) stay
    seeded as ``unsupported`` without a network round-trip.
    """
    out: dict[str, str] = {name: "unsupported" for name in _ALL_NODES}
    client = getattr(backend, "_client", None)
    if client is None:
        if include_multimodal:
            out[_NODE_MULTIMODAL_INPUT] = await probe_multimodal_support(backend)
        return out
    params = {"key": getattr(backend, "api_key", "")}
    base_url = str(getattr(backend, "base_url", "")).rstrip("/")
    model = getattr(backend, "model", "gemini-2.5-pro")

    async def _post(path: str, body: dict[str, Any]) -> None:
        resp = await client.post(f"{base_url}{path}", params=params, json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")

    async def _get(path: str) -> None:
        resp = await client.get(f"{base_url}{path}", params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")

    out[_NODE_CHAT] = await _node_supported(_post(f"/models/{model}:generateContent", {
            "contents": [{"parts": [{"text": "hi"}]}]}))
    out[_NODE_MODELS] = await _node_supported(_get("/models"))
    out[_NODE_EMBEDDINGS] = await _node_supported(_post("/models/text-embedding-005:batchEmbedContents", {
            "requests": [{"content": {"parts": [{"text": "hi"}]}}]}))
    out[_NODE_FILES] = await _node_supported(_get("/files"))
    out[_NODE_BATCHES] = await _node_supported(_post(f"/models/{model}:batchGenerateContent", {
            "batch": {
                "display_name": "encre-probe",
                "input_config": {"requests": {"requests": [
                    {"request": {"contents": [{"parts": [{"text": "hi"}]}]},
                     "metadata": {"custom_id": "probe"}}
                ]}},
            }}))
    out[_NODE_FINE_TUNING] = await _node_supported(_get("/tunedModels"))

    if include_multimodal:
        out[_NODE_MULTIMODAL_INPUT] = await probe_multimodal_support(backend)
    return out


async def _consume_chat_with_data(
    backend: Any, messages: list[dict[str, Any]], max_tokens: int = 8
) -> bool:
    """Run ``backend.chat(...)`` and report whether it produced content.

    Handles async generators (the common case), awaitables and plain sync
    iterables.  An explicit backend error is treated as a failure.
    """
    result = backend.chat(messages=messages, max_tokens=max_tokens, stream=False)

    async def _scan_asyncgen(agn: Any) -> bool:
        saw_success = False
        saw_error = False
        async for ev in agn:
            if getattr(ev, "error", None):
                saw_error = True
            elif hasattr(ev, "text") or getattr(ev, "finish_reason", None) is not None:
                saw_success = True
        return saw_success and not saw_error

    if inspect.isasyncgen(result):
        return await _scan_asyncgen(result)
    if inspect.isawaitable(result):
        try:
            result = await result
        except Exception:
            return False
        if inspect.isasyncgen(result):
            return await _scan_asyncgen(result)
        return result is not None
    # Sync generator or list of events.
    try:
        saw_success = False
        saw_error = False
        for ev in result:
            if getattr(ev, "error", None):
                saw_error = True
            elif hasattr(ev, "text") or getattr(ev, "finish_reason", None) is not None:
                saw_success = True
        return saw_success and not saw_error
    except Exception:
        return result is not None


async def _probe_common_protocol(backend: Any) -> str:
    """Fallback multimodal probe: pass a 1x1 PNG into the backend's own chat.

    Works for method-level backends with no fixed HTTP protocol surface
    (Bedrock / Local / Failover / Router, ...).
    """
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "what is in this image?"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{_PROBE_PNG_B64}"}},
        ],
    }]
    try:
        if await _consume_chat_with_data(backend, messages):
            return "supported"
    except Exception:
        logger.debug("exception suppressed", exc_info=True)
    return "unsupported"


async def _probe_common_nodes(backend: Any, include_multimodal: bool) -> dict[str, str]:
    """Generic method-level prober for backends without a probeable HTTP
    protocol (Bedrock / Local / Failover / Router / unknown).  Each node is
    verified by calling the backend's own public method with a minimal
    payload; any failure (NotImplementedError, auth, timeout) is recorded as
    ``unsupported``.  Write-heavy endpoints (files / batches / fine-tuning /
    realtime / responses) are judged from the declared ``supports_*`` flags,
    which are the authoritative capability signal for SDK-style backends.
    """
    out: dict[str, str] = {name: "unsupported" for name in _ALL_NODES}

    def _flag(name: str) -> bool:
        method = getattr(backend, f"supports_{name}", None)
        if method is None:
            return False
        try:
            return bool(method())
        except Exception:
            return False

    async def _call(name: str, args: tuple[Any, ...], kwargs: dict[str, Any], node: str) -> None:
        method = getattr(backend, name, None)
        if method is None:
            return
        try:
            result = method(*args, **kwargs)
            if inspect.isawaitable(result):
                await result
            out[node] = "supported"
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

    # Chat exists on every backend and is the core node.
    if await _consume_chat_with_data(backend, [{
            "role": "user", "content": "hi"}]):
        out[_NODE_CHAT] = "supported"

    if hasattr(backend, "list_models"):
        try:
            if (await backend.list_models()) is not None:
                out[_NODE_MODELS] = "supported"
        except Exception:
            logger.debug("exception suppressed", exc_info=True)

    # Generation / analysis nodes -- stable signatures, small safe payloads.
    await _call("generate_image", ("tiny test",), {"n": 1, "size": "256x256"}, _NODE_IMAGES_GENERATION)
    await _call("edit_image", ("tiny test", _PROBE_PNG_B64), {"size": "256x256"}, _NODE_IMAGES_EDIT)
    await _call("create_image_variation", (_PROBE_PNG_B64,), {"size": "256x256"}, _NODE_IMAGES_VARIATION)
    await _call("translate_audio", (base64.b64encode(_probe_wav_bytes()).decode(),), {}, _NODE_AUDIO_TRANSLATION)
    await _call("create_embeddings", ("hi",), {}, _NODE_EMBEDDINGS)
    await _call("create_moderation", ("hi",), {}, _NODE_MODERATION)

    # Write-heavy nodes -- judged by the declared capability flags.
    if _flag("files"):
        out[_NODE_FILES] = "supported"
    if _flag("batch"):
        out[_NODE_BATCHES] = "supported"
    if _flag("fine_tuning"):
        out[_NODE_FINE_TUNING] = "supported"
    if _flag("realtime"):
        out[_NODE_REALTIME] = "supported"
    if _flag("responses_api"):
        out[_NODE_RESPONSES] = "supported"

    if include_multimodal:
        out[_NODE_MULTIMODAL_INPUT] = await probe_multimodal_support(backend)
    return out


# ── Reasoning-level probing ────────────────────────────────

# Candidate reasoning-effort strings seen across providers.  This is a
# PROBE DICTIONARY, not a capability claim: providers change level support
# constantly, so nothing here is trusted until the live endpoint accepts
# it.  Only levels the endpoint actually accepts are reported.
_REASONING_LEVEL_CANDIDATES = ("minimal", "low", "medium", "high", "xhigh", "max")

# Constructor kwarg names through which a backend forwards the reasoning
# level.  Backends that do not map the level into any of these keys have
# no level concept at all (thinking is a plain on/off toggle for them).
_LEVEL_KWARG_KEYS = ("reasoning_effort", "thinking_level", "thinking_effort")

# Thinking probes need a budget window: Anthropic requires
# max_tokens > thinking.budget_tokens, other providers just cap output.
_PROBE_THINKING_BUDGET = 1024
_PROBE_THINKING_MAX_TOKENS = 2048


def _backend_honors_level(backend_type: str) -> bool:
    """True when the backend maps the thinking level into a request kwarg.

    Checked locally against :func:`_build_thinking_kwargs` -- no network
    involved.  Backends with an on/off-only thinking envelope report no
    reasoning levels even when thinking itself is supported.
    """
    from encre.backends.adapters import _build_thinking_kwargs
    from encre.utils.types import EnabledThinking

    try:
        kwargs = _build_thinking_kwargs(
            backend_type,
            EnabledThinking(
                enabled=True,
                budget_tokens=_PROBE_THINKING_BUDGET,
                level="medium",
                selectable=True,
            ),
        )
    except Exception:
        return False
    return any(key in kwargs for key in _LEVEL_KWARG_KEYS)


async def probe_reasoning_levels(
    backend_type: str,
    api_key: str,
    base_url: str,
    model_id: str,
    headers: dict[str, str] | None = None,
    sampling: dict[str, float | int] | None = None,
) -> tuple[list[str], str]:
    """Verify reasoning levels live against the provider endpoint.

    Every candidate level is probed with a minimal thinking-enabled chat
    request; a level counts as verified only when the endpoint accepts it.
    Returns ``(levels, support)``:

    * ``levels``  -- verified level strings, in candidate order.  Empty
      when the backend has no level concept or no level was accepted.
    * ``support`` -- "supported" (thinking accepted, with or without
      levels), "unsupported" (every thinking probe failed while the
      plain connectivity check passed) or "unknown" (probe error).
    """
    from dataclasses import replace

    from encre.backend import create_backend
    from encre.backends.retry import DEFAULT_RETRY_CONFIG
    from encre.utils.types import EnabledThinking

    # No retries during probing: a single hung level must fail fast instead
    # of turning into exponential backoff across all candidates.
    probe_retry = replace(DEFAULT_RETRY_CONFIG, max_retries=0, rate_limit_retries=0)

    def _make(level: str) -> Any:
        return create_backend(
            backend_type,
            api_key=api_key,
            base_url=base_url,
            model=model_id,
            thinking_config=EnabledThinking(
                enabled=True,
                budget_tokens=_PROBE_THINKING_BUDGET,
                level=level,
                selectable=True,
            ),
            extra_headers=dict(headers or {}) or None,
            retry_config=probe_retry,
            http_timeout=_PROBE_CHAT_TIMEOUT,
            **(sampling or {}),
        )

    honors = _backend_honors_level(backend_type)
    candidates: tuple[str, ...] = _REASONING_LEVEL_CANDIDATES if honors else ("",)
    verified: list[str] = []

    async def _probe_level(level: str) -> bool:
        """Try one candidate level and report whether the endpoint accepted it.

        Every candidate gets its own backend so an unsupported level cannot
        influence the others.  Failures (including timeouts) count as
        ``False`` and close the backend regardless.
        """
        backend = _make(level)
        if backend is None:
            return False
        try:
            return await asyncio.wait_for(
                _consume_chat_with_data(
                    backend,
                    [{"role": "user", "content": "hi"}],
                    max_tokens=_PROBE_THINKING_MAX_TOKENS,
                ),
                timeout=_PROBE_CHAT_TIMEOUT,
            )
        except Exception:
            return False
        finally:
            try:
                await backend.aclose()
            except Exception:
                logger.debug("exception suppressed", exc_info=True)

    # Candidates run concurrently: the reasoning probe must not cost one
    # chat per level, otherwise a 6-level probe is 6x the single-chat cost.
    outcomes = await asyncio.gather(*(_probe_level(level) for level in candidates))
    saw_success = False
    for level, ok in zip(candidates, outcomes, strict=False):
        if ok:
            saw_success = True
            if honors and level:
                verified.append(level)
    if saw_success:
        return verified, "supported"
    # Every thinking probe failed.  Plain connectivity was already verified
    # by the caller, so -- same judgement as the multimodal probe -- the
    # endpoint does not accept reasoning at all.
    return [], "unsupported"


class MultimodalMixin:
    """Mixin providing multimodal implementations for OpenAI-protocol backends.

    Subclasses must expose:

    * ``api_base_url: str``
    * ``api_key: str``
    * ``model: str``
    * ``_get_client() -> httpx.AsyncClient``
    * ``_build_headers() -> dict[str, str]``

    The mixin implements every multimodal method declared on
    :class:`encre.backends.base.BaseBackend` and may be combined freely
    with other mixins (e.g. failover, retry) without further changes.
    """

    # Capability flags -- subclasses can override if a provider is known
    # to support a feature under a different protocol shape.
    def supports_image_generation(self) -> bool:
        return True

    def supports_image_edit(self) -> bool:
        return True

    def supports_image_variation(self) -> bool:
        return True

    def supports_embeddings(self) -> bool:
        return True

    def supports_moderation(self) -> bool:
        return True

    def supports_files(self) -> bool:
        return True

    def supports_batch(self) -> bool:
        return True

    def supports_fine_tuning(self) -> bool:
        return True

    def supports_responses_api(self) -> bool:
        return True

    def supports_realtime(self) -> bool:
        return True

    # ── Internal helpers ────────────────────────────────────────────────

    def _multimodal_base(self) -> str:
        """Return the API root URL with any ``/chat/completions`` suffix stripped."""
        base = (self.api_base_url or "").rstrip("/")
        for suffix in ("/chat/completions",):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        return base

    def _post_json(
        self,
        _path: str,
        _payload: dict[str, Any],
        *,
        _timeout: float | None = None,
    ) -> dict[str, Any]:
        """Synchronous wrapper used internally for non-streaming JSON POSTs.

        Async callers should use :meth:`_post_json_async` instead.
        """
        raise RuntimeError("Use _post_json_async")

    async def _post_json_async(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """POST a JSON payload and return the parsed response body."""
        client = self._get_client()
        headers = self._build_headers()
        url = f"{self._multimodal_base()}{path}"
        response = await client.post(
            url,
            headers=headers,
            json=payload or {},
            timeout=timeout,
        )
        if response.status_code >= 400:
            body = response.text
            _LOG.error(
                "HTTP %s on %s: %s",
                response.status_code,
                url,
                body[:2000],
            )
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"_raw": response.text}

    async def _get_json_async(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """GET a JSON endpoint and return the parsed response body."""
        client = self._get_client()
        headers = self._build_headers()
        url = f"{self._multimodal_base()}{path}"
        response = await client.get(
            url,
            headers=headers,
            params=params or {},
            timeout=timeout,
        )
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"_raw": response.text}

    async def _delete_async(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Issue a DELETE request and return the raw response."""
        client = self._get_client()
        headers = self._build_headers()
        url = f"{self._multimodal_base()}{path}"
        response = await client.delete(
            url,
            headers=headers,
            params=params or {},
        )
        return response

    async def _post_multipart_async(
        self,
        path: str,
        data: dict[str, Any],
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST multipart/form-data and return the parsed response body."""
        client = self._get_client()
        headers = self._build_headers()
        # httpx requires the Content-Type header to be omitted for multipart.
        headers.pop("Content-Type", None)
        url = f"{self._multimodal_base()}{path}"
        response = await client.post(
            url,
            headers=headers,
            data=data,
            files=files or {},
        )
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"_raw": response.text}

    # ── Image generation ────────────────────────────────────────────────

    async def generate_image(
        self,
        prompt: str,
        model: str | None = None,
        n: int = 1,
        size: str = "1024x1024",
        quality: str = "standard",
        response_format: str = "b64_json",
        style: str | None = None,
        user: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResponse:
        """Generate one or more images using the OpenAI Images API."""
        payload: dict[str, Any] = {
            "prompt": prompt,
            "n": n,
            "size": size,
            "response_format": response_format,
        }
        if model:
            payload["model"] = model
        else:
            payload["model"] = self.model or "dall-e-3"
        if quality:
            payload["quality"] = quality
        if style:
            payload["style"] = style
        if user:
            payload["user"] = user
        if extra_params:
            payload.update(extra_params)

        data = await self._post_json_async("/images/generations", payload)
        return _parse_image_response(data, provider=self.provider_name())

    async def edit_image(
        self,
        prompt: str,
        image_b64: str,
        mask_b64: str | None = None,
        model: str | None = None,
        n: int = 1,
        size: str = "1024x1024",
        response_format: str = "b64_json",
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResponse:
        """Edit (inpaint) an image via the OpenAI Images edits endpoint."""
        form: dict[str, Any] = {
            "prompt": prompt,
            "n": str(n),
            "size": size,
            "response_format": response_format,
        }
        if model:
            form["model"] = model
        else:
            form["model"] = self.model or "dall-e-2"
        if extra_params:
            for k, v in extra_params.items():
                form[k] = str(v)

        files: dict[str, Any] = {
            "image": ("image.png", _b64_to_bytes(image_b64), "image/png"),
        }
        if mask_b64:
            files["mask"] = ("mask.png", _b64_to_bytes(mask_b64), "image/png")

        data = await self._post_multipart_async("/images/edits", form, files)
        return _parse_image_response(data, provider=self.provider_name())

    async def create_image_variation(
        self,
        image_b64: str,
        model: str | None = None,
        n: int = 1,
        size: str = "1024x1024",
        response_format: str = "b64_json",
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResponse:
        """Produce image variations via the OpenAI Images variations endpoint."""
        form: dict[str, Any] = {
            "n": str(n),
            "size": size,
            "response_format": response_format,
        }
        if model:
            form["model"] = model
        else:
            form["model"] = self.model or "dall-e-2"
        if extra_params:
            for k, v in extra_params.items():
                form[k] = str(v)

        files: dict[str, Any] = {
            "image": ("image.png", _b64_to_bytes(image_b64), "image/png"),
        }
        data = await self._post_multipart_async(
            "/images/variations", form, files
        )
        return _parse_image_response(data, provider=self.provider_name())

    # ── Audio ────────────────────────────────────────────────────────────

    async def _transcribe_or_translate(
        self,
        path: str,
        audio_b64: str,
        filename: str,
        mime_type: str,
        model: str | None,
        response_format: str,
        temperature: float,
        prompt: str | None,
        extra_params: dict[str, Any] | None,
    ) -> AudioResult:
        form: dict[str, Any] = {
            "model": model or self.model or "whisper-1",
            "response_format": response_format,
            "temperature": str(temperature),
        }
        if prompt:
            form["prompt"] = prompt
        if extra_params:
            for k, v in extra_params.items():
                form[k] = str(v)

        files: dict[str, Any] = {
            "file": (filename, _b64_to_bytes(audio_b64), mime_type),
        }
        data = await self._post_multipart_async(path, form, files)
        text_value = ""
        language = ""
        duration: float | None = None
        segments: list[dict[str, Any]] = []
        if isinstance(data, dict):
            if "text" in data:
                text_value = str(data.get("text", ""))
            elif "_raw" in data:
                text_value = str(data["_raw"])
            language = str(data.get("language", ""))
            if "duration" in data:
                try:
                    duration = float(data["duration"])
                except (TypeError, ValueError):
                    duration = None
            if "segments" in data and isinstance(data["segments"], list):
                segments = [dict(s) for s in data["segments"]]
        return AudioResult(
            text=text_value,
            language=language,
            segments=segments,
            duration=duration,
            model=form["model"],
            provider=self.provider_name(),
        )

    async def translate_audio(
        self,
        audio_b64: str,
        model: str | None = None,
        response_format: str = "json",
        temperature: float = 0.0,
        prompt: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> AudioResult:
        """Translate audio into English text via the OpenAI Whisper endpoint."""
        mime_type = "audio/mpeg"
        if extra_params and isinstance(extra_params.get("mime_type"), str):
            mime_type = extra_params["mime_type"]
        return await self._transcribe_or_translate(
            "/audio/translations",
            audio_b64,
            "audio.mp3",
            mime_type,
            model,
            response_format,
            temperature,
            prompt,
            extra_params,
        )

    # ── Embeddings ───────────────────────────────────────────────────────

    async def create_embeddings(
        self,
        input: str | list[str],
        model: str | None = None,
        encoding_format: str = "float",
        dimensions: int | None = None,
        user: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> EmbeddingResponse:
        """Generate embedding vectors using the OpenAI embeddings endpoint."""
        payload: dict[str, Any] = {
            "model": model or self.model or "text-embedding-3-small",
            "input": input,
            "encoding_format": encoding_format,
        }
        if dimensions is not None:
            payload["dimensions"] = dimensions
        if user:
            payload["user"] = user
        if extra_params:
            payload.update(extra_params)

        data = await self._post_json_async("/embeddings", payload)
        results: list[EmbeddingResult] = []
        for item in data.get("data", []) if isinstance(data, dict) else []:
            emb_raw = item.get("embedding", [])
            if isinstance(emb_raw, str):
                # base64 encoded vector
                try:
                    decoded = base64.b64decode(emb_raw)
                    # Decode as little-endian float32 list
                    import struct
                    count = len(decoded) // 4
                    # Interpret the raw bytes as ``count`` little-endian f32
                    # values (OpenAI's binary embedding format).
                    emb_raw = list(struct.unpack(f"<{count}f", decoded[: count * 4]))
                except Exception:
                    emb_raw = []
            results.append(
                EmbeddingResult(
                    index=int(item.get("index", len(results))),
                    embedding=list(emb_raw) if isinstance(emb_raw, list) else [],
                    object=str(item.get("object", "embedding")),
                    model=str(item.get("model", payload["model"])),
                )
            )
        return EmbeddingResponse(
            object=str(data.get("object", "list")) if isinstance(data, dict) else "list",
            data=results,
            model=str(data.get("model", payload["model"])) if isinstance(data, dict) else payload["model"],
            usage=dict(data.get("usage", {})) if isinstance(data, dict) else {},
            provider=self.provider_name(),
        )

    # ── Moderation ───────────────────────────────────────────────────────

    async def create_moderation(
        self,
        input: str | list[str],
        model: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> ModerationResponse:
        """Classify input text via the OpenAI moderation endpoint."""
        payload: dict[str, Any] = {
            "model": model or self.model or "omni-moderation-latest",
            "input": input,
        }
        if extra_params:
            payload.update(extra_params)

        data = await self._post_json_async("/moderations", payload)
        results: list[ModerationResult] = []
        for item in data.get("results", []) if isinstance(data, dict) else []:
            cats = item.get("categories", {}) or {}
            scores = item.get("category_scores", {}) or {}
            results.append(
                ModerationResult(
                    flagged=bool(item.get("flagged", False)),
                    categories=[
                        ModerationCategory(
                            name=str(name),
                            score=float(scores.get(name, 0.0)),
                            flagged=bool(cats.get(name, False)),
                        )
                        for name in cats
                    ],
                    category_scores={
                        str(name): float(value)
                        for name, value in scores.items()
                    },
                    input_text="",
                )
            )
        return ModerationResponse(
            id=str(data.get("id", "")) if isinstance(data, dict) else "",
            model=str(data.get("model", payload["model"])) if isinstance(data, dict) else payload["model"],
            results=results,
            provider=self.provider_name(),
        )

    # ── Files ────────────────────────────────────────────────────────────

    async def upload_file(
        self,
        filename: str,
        content_b64: str,
        purpose: str = "assistants",
        mime_type: str = "application/octet-stream",
        extra_params: dict[str, Any] | None = None,
    ) -> FileObject:
        """Upload a file via the OpenAI Files multipart endpoint."""
        form: dict[str, Any] = {"purpose": purpose}
        if extra_params:
            for k, v in extra_params.items():
                form[k] = str(v)
        files: dict[str, Any] = {
            "file": (filename, _b64_to_bytes(content_b64), mime_type),
        }
        data = await self._post_multipart_async("/files", form, files)
        return _parse_file_object(data, provider=self.provider_name())

    async def list_files(
        self,
        purpose: str | None = None,
        limit: int = 100,
        after: str | None = None,
        order: str = "desc",
        extra_params: dict[str, Any] | None = None,
    ) -> FileListResponse:
        """List files via the OpenAI Files endpoint."""
        params: dict[str, Any] = {"limit": limit, "order": order}
        if purpose:
            params["purpose"] = purpose
        if after:
            params["after"] = after
        if extra_params:
            params.update(extra_params)
        data = await self._get_json_async("/files", params=params)
        files_data = data.get("data", []) if isinstance(data, dict) else []
        return FileListResponse(
            object=str(data.get("object", "list")) if isinstance(data, dict) else "list",
            data=[_parse_file_object(item, provider=self.provider_name()) for item in files_data],
            has_more=bool(data.get("has_more", False)) if isinstance(data, dict) else False,
        )

    async def retrieve_file(
        self,
        file_id: str,
    ) -> FileObject:
        """Fetch a file's metadata by id."""
        data = await self._get_json_async(f"/files/{file_id}")
        return _parse_file_object(data, provider=self.provider_name())

    async def delete_file(
        self,
        file_id: str,
    ) -> bool:
        """Delete an uploaded file.  Returns ``True`` on success."""
        response = await self._delete_async(f"/files/{file_id}")
        if response.status_code >= 400:
            return False
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return True
        return bool(payload.get("deleted", True)) if isinstance(payload, dict) else True

    async def download_file(
        self,
        file_id: str,
    ) -> ProviderFileContent:
        """Download the raw content of a file."""
        client = self._get_client()
        headers = self._build_headers()
        url = f"{self._multimodal_base()}/files/{file_id}/content"
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        mime_type = response.headers.get("content-type", "")
        return ProviderFileContent(
            file_id=file_id,
            filename="",
            content_b64=_bytes_to_b64(response.content),
            mime_type=mime_type,
            provider=self.provider_name(),
        )

    # ── Batch ────────────────────────────────────────────────────────────

    async def _stage_batch_requests(
        self,
        requests: list[BatchRequest],
    ) -> str:
        """Serialise a list of batch requests to a JSONL blob and upload it.

        Returns the file id of the staged JSONL.
        """
        lines = []
        for r in requests:
            lines.append(
                json.dumps(
                    {
                        "custom_id": r.custom_id,
                        "method": r.method,
                        "url": r.url,
                        "body": r.body,
                    },
                    ensure_ascii=False,
                )
            )
        jsonl_b64 = _bytes_to_b64(("\n".join(lines)).encode("utf-8"))
        file_obj = await self.upload_file(
            filename="batch_input.jsonl",
            content_b64=jsonl_b64,
            purpose="batch",
            mime_type="application/jsonl",
        )
        return file_obj.id

    async def create_batch(
        self,
        requests: list[BatchRequest],
        endpoint: str = "/v1/chat/completions",
        completion_window: str = "24h",
        metadata: dict[str, Any] | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> BatchObject:
        """Create an OpenAI batch.

        The list of requests is first serialised to a JSONL blob and
        uploaded via the Files API (purpose ``batch``).  The resulting
        file id is then used to create the batch.
        """
        if not requests:
            raise ValueError("create_batch requires at least one request")

        input_file_id = await self._stage_batch_requests(requests)
        payload: dict[str, Any] = {
            "input_file_id": input_file_id,
            "endpoint": endpoint,
            "completion_window": completion_window,
        }
        if metadata:
            payload["metadata"] = metadata
        if extra_params:
            payload.update(extra_params)

        data = await self._post_json_async("/batches", payload)
        return _parse_batch_object(data, provider=self.provider_name())

    async def retrieve_batch(
        self,
        batch_id: str,
    ) -> BatchObject:
        """Fetch a batch by id."""
        data = await self._get_json_async(f"/batches/{batch_id}")
        return _parse_batch_object(data, provider=self.provider_name())

    async def list_batches(
        self,
        limit: int = 20,
        after: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> BatchListResponse:
        """List recent batches."""
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        if extra_params:
            params.update(extra_params)
        data = await self._get_json_async("/batches", params=params)
        items = data.get("data", []) if isinstance(data, dict) else []
        return BatchListResponse(
            object=str(data.get("object", "list")) if isinstance(data, dict) else "list",
            data=[_parse_batch_object(item, provider=self.provider_name()) for item in items],
            has_more=bool(data.get("has_more", False)) if isinstance(data, dict) else False,
        )

    async def cancel_batch(
        self,
        batch_id: str,
    ) -> BatchObject:
        """Cancel an in-flight batch."""
        client = self._get_client()
        headers = self._build_headers()
        url = f"{self._multimodal_base()}/batches/{batch_id}/cancel"
        response = await client.post(url, headers=headers)
        response.raise_for_status()
        data = response.json() if response.content else {}
        return _parse_batch_object(data, provider=self.provider_name())

    # ── Fine-tuning ──────────────────────────────────────────────────────

    async def create_fine_tuning_job(
        self,
        training_file: str,
        model: str,
        hyperparameters: FineTuneHyperparameters | None = None,
        validation_file: str | None = None,
        suffix: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> FineTuneJob:
        """Create a fine-tuning job."""
        payload: dict[str, Any] = {
            "training_file": training_file,
            "model": model,
        }
        if hyperparameters is not None:
            payload["hyperparameters"] = {
                k: v
                for k, v in {
                    "n_epochs": hyperparameters.n_epochs,
                    "batch_size": hyperparameters.batch_size,
                    "learning_rate_multiplier": hyperparameters.learning_rate_multiplier,
                }.items()
                if v is not None
            }
        if validation_file:
            payload["validation_file"] = validation_file
        if suffix:
            payload["suffix"] = suffix
        if extra_params:
            payload.update(extra_params)

        data = await self._post_json_async("/fine_tuning/jobs", payload)
        return _parse_fine_tune_job(data, provider=self.provider_name())

    async def retrieve_fine_tuning_job(
        self,
        job_id: str,
    ) -> FineTuneJob:
        """Fetch a fine-tuning job by id."""
        data = await self._get_json_async(f"/fine_tuning/jobs/{job_id}")
        return _parse_fine_tune_job(data, provider=self.provider_name())

    async def list_fine_tuning_jobs(
        self,
        limit: int = 20,
        after: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> FineTuneJobList:
        """List recent fine-tuning jobs."""
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        if extra_params:
            params.update(extra_params)
        data = await self._get_json_async("/fine_tuning/jobs", params=params)
        items = data.get("data", []) if isinstance(data, dict) else []
        return FineTuneJobList(
            object=str(data.get("object", "list")) if isinstance(data, dict) else "list",
            data=[_parse_fine_tune_job(item, provider=self.provider_name()) for item in items],
            has_more=bool(data.get("has_more", False)) if isinstance(data, dict) else False,
        )

    async def list_fine_tuning_events(
        self,
        job_id: str,
        limit: int = 20,
        after: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> list[FineTuneEvent]:
        """List the event log of a fine-tuning job."""
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        if extra_params:
            params.update(extra_params)
        data = await self._get_json_async(
            f"/fine_tuning/jobs/{job_id}/events",
            params=params,
        )
        items = data.get("data", []) if isinstance(data, dict) else []
        events: list[FineTuneEvent] = []
        for item in items:
            events.append(
                FineTuneEvent(
                    id=str(item.get("id", "")),
                    created_at=int(item.get("created_at", 0)),
                    level=str(item.get("level", "info")),
                    message=str(item.get("message", "")),
                    data=dict(item.get("data", {})) if isinstance(item.get("data"), dict) else {},
                )
            )
        return events

    async def cancel_fine_tuning_job(
        self,
        job_id: str,
    ) -> FineTuneJob:
        """Cancel an in-flight fine-tuning job."""
        client = self._get_client()
        headers = self._build_headers()
        url = f"{self._multimodal_base()}/fine_tuning/jobs/{job_id}/cancel"
        response = await client.post(url, headers=headers)
        response.raise_for_status()
        data = response.json() if response.content else {}
        return _parse_fine_tune_job(data, provider=self.provider_name())

    # ── Responses API ────────────────────────────────────────────────────

    async def create_response(
        self,
        input: str | list[dict[str, Any]],
        model: str | None = None,
        instructions: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
        stream: bool = False,
        background: bool = False,
        previous_response_id: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> ResponseObject:
        """Call the OpenAI Responses API.

        Streaming and background modes are passed to the provider via the
        ``stream`` / ``background`` flags.  This implementation always
        returns the final assembled :class:`ResponseObject`; incremental
        frames are not surfaced here.
        """
        if isinstance(input, str):
            payload_input: Any = input
        else:
            payload_input = list(input)

        payload: dict[str, Any] = {
            "model": model or self.model,
            "input": payload_input,
        }
        if instructions is not None:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if temperature:
            payload["temperature"] = temperature
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        if stream:
            payload["stream"] = True
        if background:
            payload["background"] = True
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id
        if extra_params:
            payload.update(extra_params)

        data = await self._post_json_async("/responses", payload)
        return _parse_response_object(data, provider=self.provider_name())

    async def retrieve_response(
        self,
        response_id: str,
    ) -> ResponseObject:
        """Fetch a previously-created response by id."""
        data = await self._get_json_async(f"/responses/{response_id}")
        return _parse_response_object(data, provider=self.provider_name())

    async def delete_response(
        self,
        response_id: str,
    ) -> bool:
        """Delete a previously-created response by id."""
        response = await self._delete_async(f"/responses/{response_id}")
        if response.status_code >= 400:
            return False
        try:
            payload = response.json()
        except json.JSONDecodeError:
            return True
        return bool(payload.get("deleted", True)) if isinstance(payload, dict) else True

    # ── Realtime ─────────────────────────────────────────────────────────

    async def create_realtime_session(
        self,
        config: RealtimeSessionConfig | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> RealtimeSession:
        """Open a Realtime WebSocket session.

        The OpenAI Realtime API exposes a single ``wss://`` endpoint
        (``/v1/realtime``) and a separate ``POST /v1/realtime/sessions``
        endpoint to create a session token.  We call the latter and then
        open the WebSocket using ``httpx_ws`` if available, otherwise we
        return a session descriptor with ``transport=None`` and let the
        caller wire up a transport of their choice.
        """
        cfg = config or RealtimeSessionConfig(model=self.model)
        payload: dict[str, Any] = {
            "model": cfg.model or self.model,
            "voice": cfg.voice,
            "modalities": cfg.modalities,
            "instructions": cfg.instructions,
            "input_audio_format": cfg.input_audio_format,
            "output_audio_format": cfg.output_audio_format,
            "temperature": cfg.temperature,
            "max_response_output_tokens": cfg.max_response_output_tokens,
        }
        if cfg.turn_detection is not None:
            payload["turn_detection"] = cfg.turn_detection
        if cfg.tools:
            payload["tools"] = cfg.tools
            payload["tool_choice"] = cfg.tool_choice
        if cfg.metadata:
            payload["metadata"] = cfg.metadata
        if extra_params:
            payload.update(extra_params)

        data = await self._post_json_async("/realtime/sessions", payload)
        session_id = str(data.get("id", "")) if isinstance(data, dict) else ""
        expires_at = int(data.get("expires_at", 0)) if isinstance(data, dict) else 0

        transport = await self._open_realtime_transport(session_id, cfg)
        return RealtimeSession(
            session_id=session_id,
            model=str(data.get("model", cfg.model)) if isinstance(data, dict) else cfg.model,
            expires_at=expires_at,
            transport=transport,
            provider=self.provider_name(),
        )

    async def _open_realtime_transport(
        self,
        _session_id: str,
        cfg: RealtimeSessionConfig,
    ) -> Any:
        """Best-effort open of a WebSocket transport for Realtime.

        Tries the optional ``httpx_ws`` package; returns ``None`` if the
        package is not installed.  The caller can wire up an alternative
        transport (websockets, aiohttp, etc.) themselves.
        """
        from httpx_ws import aconnect_ws  # type: ignore[import-not-found]

        ws_url = self._multimodal_base().replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        ws_url = f"{ws_url}/realtime?model={cfg.model or self.model}"
        headers = self._build_headers()
        try:
            return await aconnect_ws(ws_url, headers=headers)
        except Exception as exc:  # pragma: no cover - network dependent
            _LOG.warning("Failed to open realtime websocket: %s", exc)
            return None

    async def close_realtime_session(
        self,
        session: RealtimeSession,
    ) -> None:
        """Close a previously-opened Realtime session."""
        transport = session.transport
        if transport is None:
            return
        close = getattr(transport, "aclose", None) or getattr(transport, "close", None)
        if close is None:
            return
        result = close()
        if asyncio.iscoroutine(result):
            await result

    # ── Provider name helper ────────────────────────────────────────────

    def provider_name(self) -> str:
        """Return the provider identifier used for result tagging.

        Backends can override this to return their own name; the default
        uses the class name (``OpenAIBackend``, ``DeepSeekBackend`` ...).
        """
        return type(self).__name__.replace("Backend", "").lower()


# ── Parsing helpers ───────────────────────────────────────────────────


def _parse_image_response(data: Any, provider: str) -> ImageGenerationResponse:
    """Convert a raw OpenAI Images response into our normalised shape."""
    items = data.get("data", []) if isinstance(data, dict) else []
    results: list[ImageResult] = []
    for item in items:
        url = str(item.get("url", "")) if isinstance(item, dict) else ""
        b64 = str(item.get("b64_json", "")) if isinstance(item, dict) else ""
        results.append(
            ImageResult(
                url=url,
                b64_json=b64,
                revised_prompt=str(item.get("revised_prompt", "")) if isinstance(item, dict) else "",
                mime_type="image/png",
                model=str(data.get("model", "")) if isinstance(data, dict) else "",
                provider=provider,
            )
        )
    return ImageGenerationResponse(
        created=int(data.get("created", int(time.time()))) if isinstance(data, dict) else int(time.time()),
        data=results,
        provider=provider,
        model=str(data.get("model", "")) if isinstance(data, dict) else "",
    )


def _parse_file_object(data: Any, provider: str) -> FileObject:
    """Convert a raw OpenAI Files response into our normalised shape."""
    if not isinstance(data, dict):
        return FileObject(provider=provider)
    return FileObject(
        id=str(data.get("id", "")),
        object=str(data.get("object", "file")),
        bytes=int(data.get("bytes", 0)),
        created_at=int(data.get("created_at", 0)),
        filename=str(data.get("filename", "")),
        purpose=str(data.get("purpose", "")),
        status=str(data.get("status", "")),
        mime_type=str(data.get("mime_type", data.get("content_type", ""))),
        provider=provider,
        metadata={k: v for k, v in data.items() if k not in {
            "id", "object", "bytes", "created_at", "filename",
            "purpose", "status", "mime_type", "content_type",
        }},
    )


def _parse_batch_object(data: Any, provider: str) -> BatchObject:
    """Convert a raw OpenAI Batch response into our normalised shape."""
    if not isinstance(data, dict):
        return BatchObject(provider=provider)
    request_counts = data.get("request_counts", {}) or {}
    if not isinstance(request_counts, dict):
        request_counts = {}
    return BatchObject(
        id=str(data.get("id", "")),
        object=str(data.get("object", "batch")),
        status=str(data.get("status", "")),
        endpoint=str(data.get("endpoint", "")),
        input_file_id=str(data.get("input_file_id", "")),
        completion_window=str(data.get("completion_window", "")),
        created_at=int(data.get("created_at", 0)),
        expires_at=int(data.get("expires_at", 0)),
        completed_at=int(data["completed_at"]) if isinstance(data.get("completed_at"), int) else None,
        failed_at=int(data["failed_at"]) if isinstance(data.get("failed_at"), int) else None,
        request_counts={str(k): int(v) for k, v in request_counts.items() if isinstance(v, int | float)},
        output_file_id=str(data.get("output_file_id", "")),
        error_file_id=str(data.get("error_file_id", "")),
        provider=provider,
        metadata={k: v for k, v in data.items() if k not in {
            "id", "object", "status", "endpoint", "input_file_id",
            "completion_window", "created_at", "expires_at",
            "completed_at", "failed_at", "request_counts",
            "output_file_id", "error_file_id",
        }},
    )


def _parse_fine_tune_job(data: Any, provider: str) -> FineTuneJob:
    """Convert a raw OpenAI fine-tuning job response into our normalised shape."""
    if not isinstance(data, dict):
        return FineTuneJob(provider=provider)
    hyper = data.get("hyperparameters", {}) or {}
    if not isinstance(hyper, dict):
        hyper = {}
    error = data.get("error", {}) or {}
    if not isinstance(error, dict):
        error = {}
    trained_tokens = data.get("trained_tokens")
    if not isinstance(trained_tokens, int):
        trained_tokens = None
    finished_at = data.get("finished_at")
    if not isinstance(finished_at, int):
        finished_at = None
    return FineTuneJob(
        id=str(data.get("id", "")),
        object=str(data.get("object", "fine_tuning.job")),
        model=str(data.get("model", "")),
        created_at=int(data.get("created_at", 0)),
        finished_at=finished_at,
        fine_tuned_model=str(data.get("fine_tuned_model", "")),
        status=str(data.get("status", "")),
        training_file=str(data.get("training_file", "")),
        validation_file=str(data.get("validation_file", "")),
        hyperparameters={str(k): v for k, v in hyper.items()},
        trained_tokens=trained_tokens,
        error={str(k): v for k, v in error.items()},
        provider=provider,
    )


def _parse_response_object(data: Any, provider: str) -> ResponseObject:
    """Convert a raw OpenAI Responses API payload into our normalised shape."""
    if not isinstance(data, dict):
        return ResponseObject(provider=provider, raw={"_raw": data})
    output = data.get("output", []) or []
    if not isinstance(output, list):
        output = []
    output_text = ""
    for item in output:
        if isinstance(item, dict):
            for block in item.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "output_text":
                    output_text += str(block.get("text", ""))
    usage = data.get("usage", {}) or {}
    if not isinstance(usage, dict):
        usage = {}
    error = data.get("error")
    if error is not None and not isinstance(error, dict):
        error = {"message": str(error)}
    return ResponseObject(
        id=str(data.get("id", "")),
        object=str(data.get("object", "response")),
        created_at=int(data.get("created_at", 0)),
        status=str(data.get("status", "")),
        output=[dict(item) for item in output if isinstance(item, dict)],
        output_text=output_text,
        usage={str(k): v for k, v in usage.items()},
        model=str(data.get("model", "")),
        error=error,
        provider=provider,
        raw=dict(data),
    )
