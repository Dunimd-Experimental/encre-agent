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

"""
Abstract base class for all LLM backends.

Defines the :class:`BaseBackend` interface that every provider-specific backend
must implement. The core contract is the :meth:`chat` method, which accepts a
conversation history (OpenAI-format message list) and optional tool definitions,
then yields a stream of :class:`BackendEvent` items.

Lifecycle
---------
1. Instantiate the backend with provider-specific credentials and model name.
2. Call ``chat()`` in an ``async for`` loop to consume the event stream.
3. Call ``aclose()`` when done to release HTTP clients and GPU memory.

BackendEvent types emitted by chat()
-------------------------------------
- :class:`BackendText` -- a text delta (streaming chunk).
- :class:`BackendThinking` -- reasoning/thinking tokens (Anthropic, DeepSeek, Gemini).
- :class:`BackendToolCallDelta` -- partial tool call name or arguments.
- :class:`BackendToolCall` -- a complete tool call ready for execution.
- :class:`BackendFinish` -- signals the end of the response with a finish reason.
- :class:`BackendError` -- a non-recoverable error that terminated the stream.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from encre.utils.types import (
    AudioResult,
    BackendEvent,
    BatchListResponse,
    BatchObject,
    BatchRequest,
    EmbeddingResponse,
    ProviderFileContent,
    FileListResponse,
    FileObject,
    FineTuneEvent,
    FineTuneHyperparameters,
    FineTuneJob,
    FineTuneJobList,
    ImageGenerationResponse,
    ModerationResponse,
    RealtimeSession,
    RealtimeSessionConfig,
    ResponseObject,
)


def format_backend_error(exc: BaseException, prefix: str = "") -> str:
    """Render a backend/HTTP exception into a human-readable message.

    The default ``str()`` of :class:`httpx.HTTPStatusError` is
    ``"Client error '400 Bad Request' for url '...'"`` -- the actual response
    body (which usually contains the provider's diagnostic, e.g.
    ``{"error": {"message": "Incorrect API key provided", "type": "..."}}``)
    is dropped.  This helper pulls the body out so the frontend can show the
    real reason to the user, and falls back gracefully for non-HTTP errors.

    Args:
        exc: The exception raised by a backend call.
        prefix: Optional prefix prepended to the message (e.g. ``"Validation
            failed: "``).  A single space is added if the prefix does not
            already end with whitespace or punctuation.

    Returns:
        A single-line string suitable for surfacing to the user.  Long bodies
        are truncated to keep the UI readable.
    """
    body_text = ""
    # HTTP-style exceptions carry the server response under ``.response``.
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            raw = response.text
        except Exception:  # pragma: no cover - defensive
            raw = ""
        if raw:
            # Try to lift the provider's structured error message out of the
            # JSON body.  Many providers (OpenAI, Anthropic, Google, DeepSeek,
            # Groq, ...) put the user-facing text under one of these keys.
            try:
                import json
                payload = json.loads(raw)
            except Exception:
                payload = None
            if isinstance(payload, dict):
                # Providers nest the message under an "error" object or key.
                err = payload.get("error")
                msg: Any = None
                if isinstance(err, dict):
                    msg = err.get("message") or err.get("msg") or err.get("detail")
                elif isinstance(err, str):
                    msg = err
                if msg is None:
                    msg = (
                        payload.get("message")
                        or payload.get("detail")
                        or payload.get("error_description")
                    )
                if isinstance(msg, list):
                    msg = "; ".join(str(x) for x in msg)
                if msg is None and isinstance(err, dict):
                    # Last resort: stringify the whole error object.
                    msg = str(err)
                body_text = str(msg) if msg else raw
            else:
                body_text = raw
            # Keep the message bounded so the chat UI does not explode.
            if len(body_text) > 1500:
                body_text = body_text[:1500] + "...(truncated)"

    if body_text:
        status_code = getattr(response, "status_code", None) if response is not None else None
        url = getattr(request, "url", None) if (request := getattr(exc, "request", None)) is not None else None
        if status_code and url:
            rendered = f"HTTP {status_code}: {body_text} (url={url})"
        elif status_code:
            rendered = f"HTTP {status_code}: {body_text}"
        else:
            rendered = body_text
    elif response is not None:
        status_code = getattr(response, "status_code", None)
        request = getattr(exc, "request", None)
        url = getattr(request, "url", None) if request is not None else None
        suffix = f" (url={url})" if url else ""
        # Fallback to str(exc) when response.text is unavailable (e.g. after
        # streaming aclose).  Avoid duplicating "HTTP {status}:" if the
        # exception message already begins with it.
        exc_str = str(exc) or ""
        if status_code:
            prefix_str = f"HTTP {status_code}: "
            if exc_str:
                if exc_str.startswith(prefix_str):
                    rendered = f"{exc_str}{suffix}"
                else:
                    rendered = f"{prefix_str}{exc_str}{suffix}"
            else:
                rendered = f"{prefix_str}<no response body>{suffix}"
        else:
            rendered = exc_str or f"<no response body>{suffix}"
    else:
        rendered = str(exc) or type(exc).__name__

    if prefix:
        return f"{prefix} {rendered}"
    return rendered


def normalize_usage(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize provider-specific usage fields to the standard key set.

    This is the single canonical usage normalizer for ALL backends.  It maps
    both naming conventions to the standard ``input_tokens`` /
    ``output_tokens`` / ``total_tokens`` triplet and unifies prompt-cache hit
    reporting under ``cache_read_input_tokens`` so the router's cost tracker
    and telemetry can compute cache hit ratios without provider-specific
    branches:

    * OpenAI reports ``prompt_tokens`` / ``completion_tokens`` and cache hits
      under ``prompt_tokens_details.cached_tokens`` (a subset of
      ``prompt_tokens``).
    * DeepSeek reports cache hits under ``prompt_cache_hit_tokens``.
    * Gemini reports ``promptTokenCount`` / ``candidatesTokenCount`` and
      implicit-cache hits under ``cachedContentTokenCount``.
    * Anthropic already uses ``input_tokens`` / ``output_tokens`` and reports
      ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
      separately; ``cache_deleted_input_tokens`` is passed through when
      present.

    Args:
        raw: The provider usage payload (or None).

    Returns:
        A dict with standardized keys; empty when ``raw`` is not a dict.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cache_deleted_input_tokens",
    ):
        if key in raw:
            out[key] = raw[key]
    # OpenAI-style naming -> standard triplet.
    if "prompt_tokens" in raw:
        out.setdefault("input_tokens", raw["prompt_tokens"])
    if "completion_tokens" in raw:
        out.setdefault("output_tokens", raw["completion_tokens"])
    if "total_tokens" in raw:
        out.setdefault("total_tokens", raw["total_tokens"])
    cached = 0
    # OpenAI: prompt_tokens_details.cached_tokens
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached += details.get("cached_tokens", 0) or 0
    # DeepSeek: prompt_cache_hit_tokens
    cached += raw.get("prompt_cache_hit_tokens", 0) or 0
    # Gemini: cachedContentTokenCount (subset of promptTokenCount)
    cached += raw.get("cachedContentTokenCount", 0) or 0
    if cached > 0:
        out["cache_read_input_tokens"] = cached + (out.get("cache_read_input_tokens", 0) or 0)
    return out


class BaseBackend(ABC):
    """Abstract base class for LLM provider backends.

    Every backend in the ``encre.backends`` package extends this class and
    implements the abstract methods below.  The class also provides default
    implementations for optional capabilities (thinking, prompt caching, token
    counting) that subclasses may override when the provider supports them.

    Provider backends and their 2026 model support:

    +-----------------------+-----------------------------------------------+
    | Backend               | 2026 models                                   |
    +-----------------------+-----------------------------------------------+
    | OpenAIBackend         | GPT-4.1, GPT-4.1 Mini/Nano, GPT-5.x, o3,     |
    |                       | o4-mini (GPT-4o deprecated)                   |
    | AnthropicBackend      | Claude Opus 4.6/4.7, Sonnet 4.5/4.6,         |
    |                       | Haiku 4.5                                      |
    | GoogleBackend         | Gemini 2.5 Pro, Gemini 2.5 Flash              |
    | DeepSeekBackend       | DeepSeek V4-Flash, V4-Pro                     |
    |                       | (deepseek-chat/reasoner deprecated Jul 2026)  |
    | GroqBackend           | Llama 3.3 70B, Llama 4 Scout, GPT-OSS 120B   |
    | OllamaBackend         | Any model served by a local Ollama instance    |
    | LocalBackend          | Any Hugging Face transformers model            |
    | BedrockBackend        | Claude, Llama, Mistral via AWS Bedrock         |
    | OpenAICompatibleBackend| vLLM, SGLang, LiteLLM, llama.cpp, etc.       |
    +-----------------------+-----------------------------------------------+
    """

    # User-configured sampling overrides (provider "advanced config").
    # Populated generically by ``create_backend``; request builders merge
    # these into the outgoing payload when non-empty.
    sampling_overrides: dict[str, Any] = {}

    def _merge_sampling_overrides(
        self, data: dict[str, Any], mapping: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Overlay user-configured sampling params onto a request body.

        Args:
            data: The request body (mutated in place).
            mapping: Optional provider-specific key renames, e.g.
                ``{"top_p": "topP"}`` for Gemini-style bodies.  Keys absent
                from the mapping are copied verbatim.

        Returns:
            The same *data* dict for call-site convenience.
        """
        for key, value in (self.sampling_overrides or {}).items():
            data[(mapping or {}).get(key, key)] = value
        return data

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = True,
        enable_caching: bool = False,
        cache_edits_state: Any = None,
    ) -> AsyncGenerator[BackendEvent, None]:
        """Send a chat completion request and stream back events.

        This is the central method of every backend.  It accepts an OpenAI-format
        message list (``[{"role": "user", "content": "..."}, ...]``) and yields
        :class:`BackendEvent` items as the response is produced.

        Args:
            messages: Conversation history in OpenAI message format. Each message
                has ``role`` (``"system"``, ``"user"``, ``"assistant"``, ``"tool"``)
                and ``content`` (string or list of content blocks).
            tools: Optional list of tool definitions in OpenAI function-calling
                format.  When provided, the model may request tool invocations.
            tool_choice: Controls tool selection behaviour.
                ``"auto"`` -- model decides; ``"any"`` -- must use a tool;
                ``"none"`` -- no tool usage; or a specific ``{"type": "function", "function": {"name": "..."}}``.  # noqa: E501
            temperature: Sampling temperature (0.0 = deterministic, 1.0 = creative).
            max_tokens: Maximum number of tokens to generate in the response.
            stream: If True (default), yields text/tool deltas as they arrive.
                If False, yields the complete response as a single burst.
            enable_caching: If True, enables prompt caching optimisations
                (Anthropic, OpenAI, DeepSeek V4 support this).

        Yields:
            BackendEvent items: :class:`BackendText`, :class:`BackendThinking`,
            :class:`BackendToolCallDelta`, :class:`BackendToolCall`,
            :class:`BackendFinish`, or :class:`BackendError`.
        """
        ...

    @abstractmethod
    def supports_tool_calling(self) -> bool:
        """Return True if the backend/model supports function/tool calling.

        Backends that return False will have tool definitions stripped before
        the request is sent to the provider.
        """
        ...

    @abstractmethod
    def context_window_size(self) -> int:
        """Return the maximum context window size in tokens.

        This value is used by the agent loop to decide when context compaction
        is needed.  The returned value should reflect the model's actual limit,
        not a provider default.

        2026 reference values:
        - GPT-4.1 family: 1,048,576 (1M)
        - GPT-5.x: 128,000-400,000 (varies by variant)
        - Claude Opus/Sonnet 4.6: 200,000 (1M in beta)
        - Gemini 2.5 Pro: 1,048,576 (1M)
        - DeepSeek V4: 1,048,576 (1M)
        - Groq models: 131,072
        - Ollama: varies by model (default 8,192-131,072)
        """
        ...

    def supports_thinking(self) -> bool:
        """Return True if the backend can extract reasoning/thinking tokens.

        All 2026 backends support extracting ``reasoning_content`` from
        response deltas.  Whether the model actually emits thinking tokens
        is the model's decision -- the backend simply passes them through
        when present.
        """
        return True

    def supports_prompt_caching(self) -> bool:
        """Return True if the backend can request prompt caching.

        Most 2026 providers support some form of prompt caching.  The
        backend may inject cache-control headers or prefixes, but the
        provider decides whether to honor them.
        """
        return True

    def count_tokens(self, _text: str) -> int:
        """Estimate the token count for a given text string.

        Returns -1 when the backend cannot provide an accurate count (the
        default).  Subclasses that have access to a tokenizer should override
        this to return a precise count.
        """
        return -1

    async def list_models(self) -> list[str]:
        """Return the list of available model IDs from this provider.

        Default implementation returns an empty list. Subclasses that support
        OpenAI-compatible APIs override this to call ``GET /models``.
        """
        return []

    async def aclose(self) -> None:  # noqa: B027
        """Release any resources held by this backend.

        This includes closing HTTP client sessions (httpx.AsyncClient),
        shutting down thread pools (LocalBackend), and releasing GPU memory.
        Called by the agent loop when the backend is no longer needed.
        """
        pass

    # ── Capability flags ────────────────────────────────────────────────
    # Subclasses override the corresponding ``supports_*`` flag to advertise
    # multimodal / extended capabilities.  The router and registry inspect
    # these flags to decide whether a backend can satisfy a request without
    # having to perform a capability probe at call time.

    def supports_image_generation(self) -> bool:
        """Return True if the backend can generate or edit images."""
        return False

    def supports_image_edit(self) -> bool:
        """Return True if the backend can edit (inpaint) images."""
        return False

    def supports_image_variation(self) -> bool:
        """Return True if the backend can produce image variations."""
        return False

    def supports_embeddings(self) -> bool:
        """Return True if the backend can produce embedding vectors."""
        return False

    def supports_moderation(self) -> bool:
        """Return True if the backend exposes a moderation endpoint."""
        return False

    def supports_files(self) -> bool:
        """Return True if the backend exposes a file management endpoint."""
        return False

    def supports_batch(self) -> bool:
        """Return True if the backend exposes batch processing endpoints."""
        return False

    def supports_fine_tuning(self) -> bool:
        """Return True if the backend exposes fine-tuning endpoints."""
        return False

    def supports_realtime(self) -> bool:
        """Return True if the backend exposes a Realtime / WebSocket endpoint."""
        return False

    def supports_responses_api(self) -> bool:
        """Return True if the backend implements the OpenAI Responses API."""
        return False

    def supports_vision_input(self) -> bool:
        """Return True if the chat() method can accept image inputs."""
        return True

    # ── Image generation ────────────────────────────────────────────────

    async def generate_image(
        self,
        _prompt: str,
        _model: str | None = None,
        _n: int = 1,
        _size: str = "1024x1024",
        _quality: str = "standard",
        _response_format: str = "b64_json",
        _style: str | None = None,
        _user: str | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResponse:
        """Generate one or more images from a text prompt.

        Args:
            prompt: The text prompt describing the desired image.
            model: Optional provider-specific model override.  Defaults to the
                backend's primary image model.
            n: Number of images to produce (1-10 depending on provider).
            size: Image dimensions as ``"WxH"`` (e.g. ``"1024x1024"``).
            quality: Image quality hint (``"standard"`` or ``"hd"`` /
                ``"high"`` / ``"medium"`` / ``"low"`` depending on model).
            response_format: ``"b64_json"`` or ``"url"``.
            style: Optional style hint (OpenAI ``"vivid"`` / ``"natural"``).
            user: Optional end-user identifier for abuse tracking.
            extra_params: Additional provider-specific parameters.

        Returns:
            An :class:`ImageGenerationResponse` containing one
            :class:`ImageResult` per generated image.

        Raises:
            NotImplementedError: If the backend does not advertise
                ``supports_image_generation()``.
        """
        if not self.supports_image_generation():
            raise NotImplementedError(
                f"{type(self).__name__} does not support image generation"
            )
        raise NotImplementedError

    async def edit_image(
        self,
        _prompt: str,
        _image_b64: str,
        _mask_b64: str | None = None,
        _model: str | None = None,
        _n: int = 1,
        _size: str = "1024x1024",
        _response_format: str = "b64_json",
        _extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResponse:
        """Edit an image (inpaint) using a prompt and optional mask.

        Args:
            prompt: Description of the desired edit.
            image_b64: Base64-encoded source image (PNG, must be square).
            mask_b64: Optional base64-encoded mask (transparent areas are
                edited; opaque areas preserved).
            model: Optional provider-specific model override.
            n: Number of edited variants to produce.
            size: Output image dimensions as ``"WxH"``.
            response_format: ``"b64_json"`` or ``"url"``.
            extra_params: Additional provider-specific parameters.

        Returns:
            An :class:`ImageGenerationResponse` containing the edited images.
        """
        if not self.supports_image_edit():
            raise NotImplementedError(
                f"{type(self).__name__} does not support image editing"
            )
        raise NotImplementedError

    async def create_image_variation(
        self,
        _image_b64: str,
        _model: str | None = None,
        _n: int = 1,
        _size: str = "1024x1024",
        _response_format: str = "b64_json",
        _extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResponse:
        """Produce variations of a source image.

        Args:
            image_b64: Base64-encoded source image (PNG, square).
            model: Optional provider-specific model override.
            n: Number of variations to produce.
            size: Output image dimensions as ``"WxH"``.
            response_format: ``"b64_json"`` or ``"url"``.
            extra_params: Additional provider-specific parameters.

        Returns:
            An :class:`ImageGenerationResponse` containing the variations.
        """
        if not self.supports_image_variation():
            raise NotImplementedError(
                f"{type(self).__name__} does not support image variations"
            )
        raise NotImplementedError

    # ── Audio ────────────────────────────────────────────────────────────

    async def translate_audio(
        self,
        _audio_b64: str,
        _model: str | None = None,
        _response_format: str = "json",
        _temperature: float = 0.0,
        _prompt: str | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> AudioResult:
        """Translate non-English audio into English text.

        Args:
            audio_b64: Base64-encoded audio bytes.
            model: Optional provider-specific translation model override.
            response_format: ``"json"``, ``"text"``, ``"srt"`` or ``"vtt"``.
            temperature: Sampling temperature.
            prompt: Optional prompt to steer translation style.
            extra_params: Additional provider-specific parameters.

        Returns:
            An :class:`AudioResult` with the English ``text``.
        """
        if not self.supports_audio_translation():
            raise NotImplementedError(
                f"{type(self).__name__} does not support audio translation"
            )
        raise NotImplementedError

    # ── Embeddings ───────────────────────────────────────────────────────

    async def create_embeddings(
        self,
        _input: str | list[str],
        _model: str | None = None,
        _encoding_format: str = "float",
        _dimensions: int | None = None,
        _user: str | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> EmbeddingResponse:
        """Generate embedding vectors for the given text input(s).

        Args:
            input: A single string or a list of strings to embed.
            model: Optional provider-specific embedding model override.
            encoding_format: ``"float"`` or ``"base64"``.
            dimensions: Optional output dimensionality override
                (text-embedding-3-* models support this).
            user: Optional end-user identifier.
            extra_params: Additional provider-specific parameters.

        Returns:
            An :class:`EmbeddingResponse` with one :class:`EmbeddingResult`
            per input string.
        """
        if not self.supports_embeddings():
            raise NotImplementedError(
                f"{type(self).__name__} does not support embeddings"
            )
        raise NotImplementedError

    # ── Moderation ───────────────────────────────────────────────────────

    async def create_moderation(
        self,
        _input: str | list[str],
        _model: str | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> ModerationResponse:
        """Classify whether text violates the provider's content policy.

        Args:
            input: A single string or a list of strings to classify.
            model: Optional provider-specific moderation model override.
            extra_params: Additional provider-specific parameters.

        Returns:
            A :class:`ModerationResponse` containing per-input classification.
        """
        if not self.supports_moderation():
            raise NotImplementedError(
                f"{type(self).__name__} does not support moderation"
            )
        raise NotImplementedError

    # ── Files ────────────────────────────────────────────────────────────

    async def upload_file(
        self,
        _filename: str,
        _content_b64: str,
        _purpose: str = "assistants",
        _mime_type: str = "application/octet-stream",
        _extra_params: dict[str, Any] | None = None,
    ) -> FileObject:
        """Upload a file to the provider's storage.

        Args:
            filename: Display filename.
            content_b64: Base64-encoded file content.
            purpose: Intended purpose (e.g. ``"assistants"``,
                ``"fine-tune"``, ``"vision"``, ``"batch"``,
                ``"user_data"``, ``"evals"``).
            mime_type: MIME type of the content.
            extra_params: Additional provider-specific parameters.

        Returns:
            A :class:`FileObject` with the provider-assigned id.
        """
        if not self.supports_files():
            raise NotImplementedError(
                f"{type(self).__name__} does not support file uploads"
            )
        raise NotImplementedError

    async def list_files(
        self,
        _purpose: str | None = None,
        _limit: int = 100,
        _after: str | None = None,
        _order: str = "desc",
        _extra_params: dict[str, Any] | None = None,
    ) -> FileListResponse:
        """List files previously uploaded to the provider.

        Args:
            purpose: Optional purpose filter.
            limit: Maximum number of files to return (1-10000).
            after: Cursor for pagination (``file_id`` after which to list).
            order: Sort order (``"asc"`` or ``"desc"``).
            extra_params: Additional provider-specific parameters.

        Returns:
            A :class:`FileListResponse` containing matching files.
        """
        if not self.supports_files():
            raise NotImplementedError(
                f"{type(self).__name__} does not support file listing"
            )
        raise NotImplementedError

    async def retrieve_file(self, _file_id: str) -> FileObject:
        """Fetch metadata for a single uploaded file.

        Args:
            file_id: Provider-assigned file id.

        Returns:
            The matching :class:`FileObject`.
        """
        if not self.supports_files():
            raise NotImplementedError(
                f"{type(self).__name__} does not support file retrieval"
            )
        raise NotImplementedError

    async def delete_file(self, _file_id: str) -> bool:
        """Delete an uploaded file.

        Args:
            file_id: Provider-assigned file id.

        Returns:
            ``True`` if the deletion succeeded.
        """
        if not self.supports_files():
            raise NotImplementedError(
                f"{type(self).__name__} does not support file deletion"
            )
        raise NotImplementedError

    async def download_file(self, _file_id: str) -> ProviderFileContent:
        """Download the raw content of an uploaded file.

        Args:
            file_id: Provider-assigned file id.

        Returns:
            A :class:`ProviderFileContent` with the file bytes base64-encoded.
        """
        if not self.supports_files():
            raise NotImplementedError(
                f"{type(self).__name__} does not support file download"
            )
        raise NotImplementedError

    # ── Batch ────────────────────────────────────────────────────────────

    async def create_batch(
        self,
        _requests: list[BatchRequest],
        _endpoint: str = "/v1/chat/completions",
        _completion_window: str = "24h",
        _metadata: dict[str, Any] | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> BatchObject:
        """Create a batch of API requests for asynchronous processing.

        Args:
            requests: The list of requests to execute.  Providers that
                require an input file (OpenAI) will stage the requests to
                an internal file and submit it transparently.
            endpoint: Provider endpoint that the batch should target
                (``/v1/chat/completions``, ``/v1/embeddings``,
                ``/v1/messages``).
            completion_window: Completion deadline window (``"24h"``).
            metadata: Optional provider-side metadata.
            extra_params: Additional provider-specific parameters.

        Returns:
            The newly-created :class:`BatchObject`.
        """
        if not self.supports_batch():
            raise NotImplementedError(
                f"{type(self).__name__} does not support batch processing"
            )
        raise NotImplementedError

    async def retrieve_batch(
        self,
        _batch_id: str,
    ) -> BatchObject:
        """Fetch the current state of a batch.

        Args:
            batch_id: Provider-assigned batch id.

        Returns:
            The current :class:`BatchObject`.
        """
        if not self.supports_batch():
            raise NotImplementedError(
                f"{type(self).__name__} does not support batch processing"
            )
        raise NotImplementedError

    async def list_batches(
        self,
        _limit: int = 20,
        _after: str | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> BatchListResponse:
        """List the most recent batches.

        Args:
            limit: Maximum number of batches to return.
            after: Cursor for pagination.
            extra_params: Additional provider-specific parameters.

        Returns:
            A :class:`BatchListResponse`.
        """
        if not self.supports_batch():
            raise NotImplementedError(
                f"{type(self).__name__} does not support batch processing"
            )
        raise NotImplementedError

    async def cancel_batch(self, _batch_id: str) -> BatchObject:
        """Cancel an in-flight batch.

        Args:
            batch_id: Provider-assigned batch id.

        Returns:
            The updated :class:`BatchObject` (typically with
            ``status="cancelling"`` or ``"cancelled"``).
        """
        if not self.supports_batch():
            raise NotImplementedError(
                f"{type(self).__name__} does not support batch processing"
            )
        raise NotImplementedError

    # ── Fine-tuning ──────────────────────────────────────────────────────

    async def create_fine_tuning_job(
        self,
        _training_file: str,
        _model: str,
        _hyperparameters: FineTuneHyperparameters | None = None,
        _validation_file: str | None = None,
        _suffix: str | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> FineTuneJob:
        """Create a supervised fine-tuning job.

        Args:
            training_file: Provider file id of the JSONL training dataset.
            model: Base model to fine-tune (e.g. ``"gpt-4.1-mini-2026-04-01"``).
            hyperparameters: Optional training hyperparameters.
            validation_file: Optional provider file id of the JSONL
                validation dataset.
            suffix: Optional suffix appended to the resulting model name.
            extra_params: Additional provider-specific parameters.

        Returns:
            The newly-created :class:`FineTuneJob`.
        """
        if not self.supports_fine_tuning():
            raise NotImplementedError(
                f"{type(self).__name__} does not support fine-tuning"
            )
        raise NotImplementedError

    async def retrieve_fine_tuning_job(self, _job_id: str) -> FineTuneJob:
        """Fetch a fine-tuning job by id."""
        if not self.supports_fine_tuning():
            raise NotImplementedError(
                f"{type(self).__name__} does not support fine-tuning"
            )
        raise NotImplementedError

    async def list_fine_tuning_jobs(
        self,
        _limit: int = 20,
        _after: str | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> FineTuneJobList:
        """List the most recent fine-tuning jobs."""
        if not self.supports_fine_tuning():
            raise NotImplementedError(
                f"{type(self).__name__} does not support fine-tuning"
            )
        raise NotImplementedError

    async def list_fine_tuning_events(
        self,
        _job_id: str,
        _limit: int = 20,
        _after: str | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> list[FineTuneEvent]:
        """List the event log of a fine-tuning job."""
        if not self.supports_fine_tuning():
            raise NotImplementedError(
                f"{type(self).__name__} does not support fine-tuning"
            )
        raise NotImplementedError

    async def cancel_fine_tuning_job(self, _job_id: str) -> FineTuneJob:
        """Cancel an in-flight fine-tuning job."""
        if not self.supports_fine_tuning():
            raise NotImplementedError(
                f"{type(self).__name__} does not support fine-tuning"
            )
        raise NotImplementedError

    # ── Realtime ─────────────────────────────────────────────────────────

    async def create_realtime_session(
        self,
        _config: RealtimeSessionConfig | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> RealtimeSession:
        """Open a Realtime (WebSocket) session with the provider.

        Args:
            config: Session configuration (model, voice, modalities, tools).
            extra_params: Additional provider-specific parameters.

        Returns:
            A :class:`RealtimeSession` holding the live ``transport``.
        """
        if not self.supports_realtime():
            raise NotImplementedError(
                f"{type(self).__name__} does not support realtime sessions"
            )
        raise NotImplementedError

    async def close_realtime_session(self, _session: RealtimeSession) -> None:
        """Close a previously-opened Realtime session."""
        if not self.supports_realtime():
            raise NotImplementedError(
                f"{type(self).__name__} does not support realtime sessions"
            )
        raise NotImplementedError

    # ── Responses API ────────────────────────────────────────────────────

    async def create_response(
        self,
        _input: str | list[dict[str, Any]],
        _model: str | None = None,
        _instructions: str | None = None,
        _tools: list[dict[str, Any]] | None = None,
        _tool_choice: str = "auto",
        _temperature: float = 0.0,
        _max_output_tokens: int | None = None,
        _stream: bool = False,
        _background: bool = False,
        _previous_response_id: str | None = None,
        _extra_params: dict[str, Any] | None = None,
    ) -> ResponseObject:
        """Call the OpenAI Responses API (unified chat + tools endpoint).

        Args:
            input: Either a single user message string or a list of input
                items in Responses API format.
            model: Optional model override.
            instructions: Optional system instructions.
            tools: Optional tool definitions.
            tool_choice: Tool selection strategy.
            temperature: Sampling temperature.
            max_output_tokens: Optional cap on generated tokens.
            stream: Whether to stream incremental events.
            background: Whether to run the request in the background.
            previous_response_id: Optional id of the prior response in the
                same logical conversation.
            extra_params: Additional provider-specific parameters.

        Returns:
            A :class:`ResponseObject` representing the response.
        """
        if not self.supports_responses_api():
            raise NotImplementedError(
                f"{type(self).__name__} does not implement the Responses API"
            )
        raise NotImplementedError

    async def retrieve_response(self, _response_id: str) -> ResponseObject:
        """Fetch a previously-created response by id."""
        if not self.supports_responses_api():
            raise NotImplementedError(
                f"{type(self).__name__} does not implement the Responses API"
            )
        raise NotImplementedError

    async def delete_response(self, _response_id: str) -> bool:
        """Delete a previously-created response by id."""
        if not self.supports_responses_api():
            raise NotImplementedError(
                f"{type(self).__name__} does not implement the Responses API"
            )
        raise NotImplementedError
