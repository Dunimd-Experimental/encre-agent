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
Shared SSE (Server-Sent Events) streaming backend for OpenAI-compatible APIs.

This module provides :class:`OpenAISSEBackend`, a reusable base class that
implements the common SSE streaming protocol shared by OpenAI, DeepSeek, Groq,
and any OpenAI-compatible provider.  Subclasses only need to configure the
API endpoint, authentication, and model name -- the SSE parsing, tool call
buffering, and non-stream fallback are all handled here.

Protocol details
----------------
The OpenAI chat completions SSE protocol emits ``data:`` lines with these
event types:

- ``data: {"choices":[{"delta":{"content":"Hello"}}]}`` -- text delta
- ``data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"search","arguments":""}}]}}]}`` -- tool call start  # noqa: E501
- ``data: {"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\"q\":"}}]}}]}`` -- tool call argument delta  # noqa: E501
- ``data: {"choices":[{"delta":{}}]}`` -- empty delta (signals end of a choice)
- ``data: [DONE]`` -- stream complete
- ``data: {"error":{...}}`` -- error event

Tool call buffering
-------------------
Tool call arguments arrive as a sequence of string deltas that must be
concatenated and parsed as JSON.  The backend buffers these deltas in a
per-request buffer, keyed by tool call index, and emits a complete
:class:`BackendToolCall` only when the stream finishes or the choice ends.

Non-stream fallback
-------------------
When ``stream=False``, the backend sends a non-streaming request and parses
the single JSON response body into the same :class:`BackendEvent` types,
ensuring the agent loop always receives a uniform event stream regardless
of the streaming setting.
"""

import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from encre.backends.auth import AuthManager
from encre.backends.base import BaseBackend, normalize_usage
from encre.backends.connection import (
    ConnectionHealthMonitor,
    format_connection_error,
)
from encre.backends.multimodal import MultimodalMixin
from encre.backends.retry import DEFAULT_RETRY_CONFIG, RetryConfig, retry_with_backoff
from encre.utils.types import (
    BackendError,
    BackendEvent,
    BackendFinish,
    BackendText,
    BackendToolCall,
    BackendToolCallDelta,
)


# ``normalize_usage`` (the single canonical usage normalizer) lives in
# ``encre.backends.base`` and is shared by all backends.


# Field names that different providers use for chain-of-thought / reasoning
# tokens in OpenAI-compatible SSE deltas and non-stream messages.
# Order matters: earlier entries are tried first.  ``reasoning_content`` is
# the de-facto standard (DeepSeek, GLM, Kimi, Qwen, Hunyuan, Doubao, Xiaomi
# MiMo all use it).  ``reasoning`` is used by OpenRouter, ``thinking`` /
# ``thought`` / ``thoughts`` by some local servers and Ollama-style bridges,
# and ``chain_of_thought`` / ``internal_thought`` by a few niche providers.
_REASONING_FIELD_NAMES: tuple[str, ...] = (
    "reasoning_content",
    "reasoning",
    "reasoning_text",
    "reasoning_output",
    "thinking",
    "thought",
    "thoughts",
    "chain_of_thought",
    "internal_thought",
    "cot",
)


def _extract_reasoning_text(obj: Any) -> str:
    """Return the first non-empty reasoning text found in ``obj``.

    Walks :data:`_REASONING_FIELD_NAMES` in order and returns the first
    value that is a non-empty string.  Returns ``""`` when nothing matches
    or when ``obj`` is not a dict.
    """
    if not isinstance(obj, dict):
        return ""
    for key in _REASONING_FIELD_NAMES:
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
        # Some providers nest the text under a sub-dict, e.g.
        # ``{"reasoning": {"text": "..."}}`` or
        # ``{"thinking": {"text": "..."}}``.
        if isinstance(value, dict):
            inner = value.get("text") or value.get("content")
            if isinstance(inner, str) and inner:
                return inner
    return ""


# ── Outbound message sanitization (single source of truth) ────────────

# Protocol fields each OpenAI-format role accepts.  Encre-internal fields
# (``id``, ``branch_id``, ``seq_in_branch``, ``parent_id``, ``usage``,
# ``segments``, ``created_at``, ``_client_id``, ``is_error``,
# ``sub_agent_messages``, ``errorMessage`` ...) must never reach the wire:
# strict providers reject unknown message fields with HTTP 400.
# ``reasoning_content`` is kept on assistant messages when thinking is
# enabled -- official provider rules (api-docs.deepseek.com "Thinking
# Mode", docs.bigmodel.cn "Thinking Mode", platform.kimi.ai "Thinking
# Models"): assistant messages that carry tool_calls MUST have their
# ``reasoning_content`` passed back in all subsequent requests (HTTP 400
# "must be passed back" otherwise), and every provider that emits it
# tolerates or ignores it elsewhere.
_OPENAI_MSG_FIELDS: dict[str, frozenset[str]] = {
    "system": frozenset({"role", "content", "name"}),
    "user": frozenset({"role", "content", "name"}),
    "assistant": frozenset({"role", "content", "name", "tool_calls", "reasoning_content"}),
    "tool": frozenset({"role", "content", "tool_call_id"}),
}

# Fields allowed inside an OpenAI ``tool_calls`` item.
_OPENAI_TOOL_CALL_FIELDS: frozenset[str] = frozenset({"id", "type", "function"})


def _coerce_openai_content(content: Any) -> Any:
    """Return a string or list content block; replace ``None`` with ``""``.

    ``None`` content is accepted by OpenAI but rejected by several
    OpenAI-compatible providers (DeepSeek rejects it on assistant
    messages that carry ``tool_calls``).
    """
    if content is None:
        return ""
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return content
    return str(content)


def sanitize_openai_messages(
    messages: list[dict[str, Any]],
    keep_reasoning: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Project Encre history onto the strict OpenAI wire format.

    The single choke point shared by every OpenAI-protocol backend
    (OpenAI, DeepSeek, Kimi, GLM, Volcengine, Groq, Ollama, generic
    compatible endpoints, ...).  For each message it keeps only the
    protocol fields of its role, coerces ``None`` content to ``""``,
    and rebuilds ``tool_calls`` items down to ``id`` / ``type`` /
    ``function`` (with string arguments, ``"{}"`` when missing).

    ``keep_reasoning`` controls whether the assistant ``reasoning_content``
    field is forwarded.  It must be True whenever the request enables
    thinking: DeepSeek requires the reasoning of tool-call assistants to
    be passed back in ALL subsequent requests and returns HTTP 400
    "The ``reasoning_content`` in the thinking mode must be passed back
    to the API" otherwise; GLM interleaved thinking and Kimi Preserved
    Thinking have the same requirement.  Non-thinking requests keep it
    stripped because legacy reasoning endpoints reject the field.

    Returns ``(projected_messages, effective_keep_reasoning)``.  The
    effective flag widens to True when the history itself carries
    reasoning: reasoning in the stored session proves the conversation
    has been running server-side thinking (possibly on a previous
    backend -- model switches keep the session, so the flag must
    survive backend changes), and stripping it then would 400 the very
    endpoints that require the replay.

    Internal-only fields (``reasoning_blocks``, ``segments``,
    ``branch_id``, ``usage``, ``_client_id``, ...) never pass through:
    only whitelisted role fields survive, so a session written by a
    native-protocol backend (Anthropic ``reasoning_blocks``) can be
    replayed on any OpenAI-protocol backend without leaking foreign
    shapes.

    The input list is never mutated; unknown-role dicts pass through
    unchanged so exotic message shapes are not silently destroyed.
    """
    if not keep_reasoning:
        keep_reasoning = any(
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and (m.get("reasoning_content") or m.get("reasoning_blocks"))
            for m in messages
        )
    out: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", ""))
        allowed = _OPENAI_MSG_FIELDS.get(role)
        if allowed is None:
            out.append(msg)
            continue
        clean: dict[str, Any] = {k: v for k, v in msg.items() if k in allowed}
        clean["role"] = role
        clean["content"] = _coerce_openai_content(msg.get("content"))
        if role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                clean_calls: list[dict[str, Any]] = []
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    clean_tc = {k: v for k, v in tc.items() if k in _OPENAI_TOOL_CALL_FIELDS}
                    func = tc.get("function")
                    if isinstance(func, dict):
                        args = func.get("arguments")
                        if args is None:
                            args = "{}"
                        elif not isinstance(args, str):
                            args = str(args)
                        clean_tc["function"] = {
                            "name": str(func.get("name", "")),
                            "arguments": args,
                        }
                    clean_calls.append(clean_tc)
                clean["tool_calls"] = clean_calls
            else:
                # Drop empty/absent tool_calls: several providers reject an
                # assistant message carrying an empty ``tool_calls: []``.
                clean.pop("tool_calls", None)
            reasoning = msg.get("reasoning_content")
            if keep_reasoning and isinstance(reasoning, str) and reasoning:
                clean["reasoning_content"] = reasoning
        out.append(clean)
    return out, keep_reasoning


class OpenAISSEBackend(MultimodalMixin, BaseBackend):
    """Base backend for OpenAI-protocol SSE streaming.

    Handles the common SSE parsing, tool call buffering, and non-stream
    fallback logic shared by OpenAI, DeepSeek, Groq, and OpenAI-compatible
    providers.  Subclasses set :attr:`api_base_url`, :attr:`api_key`, and
    :attr:`model` in their ``__init__``, then call :meth:`chat` to stream
    responses.

    Multimodal capabilities (image generation, audio TTS/STT/translation,
    embeddings, moderation, files, batch, fine-tuning, Realtime, the
    Responses API) are provided by :class:`MultimodalMixin`.

    The backend uses :class:`httpx.AsyncClient` for HTTP communication and
    :func:`retry_with_backoff` for automatic retry of transient errors.

    Subclass responsibilities:
        - Set ``self.api_base_url``, ``self.api_key``, ``self.model``.
        - Override :meth:`context_window_size` if the model has a non-default
          context window.
        - Optionally override :meth:`_prepare_request_kwargs` to add
          provider-specific request parameters.
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        retry_config: RetryConfig = DEFAULT_RETRY_CONFIG,
        http_timeout: float = 120.0,
        auth_manager: AuthManager | None = None,
        connection_monitor: ConnectionHealthMonitor | None = None,
        fallback_keys: list[str] | None = None,
        reasoning_effort: str = "",
        thinking_enabled: bool = True,
        extra_headers: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> None:
        """Initialise the SSE backend.

        Args:
            api_key: API key for authentication.  May be empty for local
                providers (Ollama, Local).
            base_url: Base URL of the API endpoint.  If empty, the subclass
                should set ``self.api_base_url`` in its own ``__init__``.
            model: Model name to use for completions.
            retry_config: :class:`RetryConfig` for transient error retries.
            http_timeout: HTTP request timeout in seconds.  Default 120s
                (covers long-thinking models like o3 and Claude Opus).
            auth_manager: Pre-configured :class:`AuthManager` for key rotation.
                If not provided but ``fallback_keys`` is given, one is created
                automatically.
            connection_monitor: Pre-configured :class:`ConnectionHealthMonitor`.
                If not provided, a default one is created.
            fallback_keys: Additional API keys for automatic rotation on
                401/403 errors.  Ignored when ``auth_manager`` is given.
            thinking_enabled: Whether to request model thinking/reasoning
                tokens.  Backends that support toggling thinking use this
                to emit ``{"thinking": {"type": "enabled"}}`` (default) or
                ``{"thinking": {"type": "disabled"}}``.
            **kwargs: Additional provider-specific parameters.
        """
        self.api_key = api_key
        self.api_base_url = base_url.rstrip("/").removesuffix("/chat/completions")
        # Normalise so the endpoint is always the API root (no trailing path).
        self.model = model
        self.http_timeout = http_timeout
        self.reasoning_effort = reasoning_effort
        self.thinking_enabled = thinking_enabled
        # Runtime observation: True once any response from this provider
        # carried thinking content (``reasoning_content`` / ``reasoning`` /
        # ``thinking`` / ``thought``).  This is the only reliable signal
        # that the server-side thinking mode is active -- model-name
        # heuristics break the moment a user aliases a model (e.g. points
        # "PRO5" at a MiniMax endpoint), so thinking replay is driven by
        # what the server actually does, never by hardcoded names.
        self._server_thinking_observed = False
        # Custom per-provider HTTP headers (OpenCode's ``options.headers``
        # equivalent).  Merged into every request with user values winning
        # over the built-in defaults (Content-Type / Authorization).
        self.extra_headers: dict[str, str] = dict(extra_headers or {})
        self._client: httpx.AsyncClient | None = None

        # -- Auth management --
        if auth_manager is not None:
            self.auth_manager = auth_manager
        elif fallback_keys:
            self.auth_manager = AuthManager(
                provider=self.api_base_url or "openai",
                api_key=api_key,
                fallback_keys=fallback_keys,
            )
        else:
            self.auth_manager = None

        if self.auth_manager is not None and retry_config.on_auth_required is None:
            from dataclasses import replace
            self.retry_config: RetryConfig = replace(
                retry_config,
                on_auth_required=self.auth_manager.refresh,
            )
        else:
            self.retry_config = retry_config

        # -- Connection health monitor --
        self.connection_monitor = connection_monitor or ConnectionHealthMonitor()

    def _get_client(self) -> httpx.AsyncClient:
        """Return (or create) the shared HTTP client.

        Uses a lazy initialisation pattern so that the client is only created
        when the first request is made.  The client is configured with:
        - A default timeout of ``self.http_timeout`` seconds.
        - No environment proxy override (``trust_env=False``) for security.
        """
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=30.0,   # 30s for cold-start APIs (DeepSeek etc.)
                    read=self.http_timeout,
                    write=30.0,
                    pool=30.0,
                ),
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        """Close the HTTP client and release all connections."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Model listing ──────────────────────────────────────────────────

    async def list_models(self) -> list[str]:
        """Fetch available models from the provider's GET /models endpoint.

        Results are cached with a 5-minute TTL to avoid excessive API calls.
        """
        import time
        now = time.time()
        cache_key = f"{self.api_base_url}:{self.api_key[:8] if self.api_key else 'noauth'}"
        if (
            hasattr(self, "_models_cache")
            and hasattr(self, "_models_cache_ts")
            and cache_key == getattr(self, "_models_cache_key", "")
            and now - self._models_cache_ts < 300
        ):
            return self._models_cache  # type: ignore[attr-defined]

        client = self._get_client()
        headers = self._build_headers()
        try:
            response = await client.get(
                f"{self.api_base_url}/models",
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            models: list[str] = []
            for item in data.get("data", []):
                mid = item.get("id", "")
                if mid:
                    models.append(mid)
            # Sort user-facing models first, then others
            models.sort()
        except Exception:
            models = []

        self._models_cache = models
        self._models_cache_ts = now
        self._models_cache_key = cache_key
        return models

    def supports_tool_calling(self) -> bool:
        """All OpenAI-protocol backends support tool calling by default."""
        return True

    def context_window_size(self) -> int:
        """Return the default context window size (128K tokens).

        Subclasses should override this to return the actual model's context
        window (e.g. 1,048,576 for GPT-4.1 or DeepSeek V4).
        """
        return 128000

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for the API request.

        Includes the ``Authorization`` header when ``self.api_key`` is set,
        and always includes ``Content-Type: application/json``.
        When an :class:`AuthManager` is configured, the current active key
        is used (allowing key rotation to take effect).  Custom
        ``extra_headers`` are merged last, so user values override the
        built-in defaults per-header.
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        current_key = (
            self.auth_manager.api_key
            if self.auth_manager is not None
            else self.api_key
        )
        if current_key:
            headers["Authorization"] = f"Bearer {current_key}"
        if self.extra_headers:
            headers.update(self.extra_headers)
        return headers

    @property
    def _keep_reasoning_content(self) -> bool:
        """Whether assistant ``reasoning_content`` is forwarded upstream.

        Driven purely by runtime facts, never by model-name heuristics:

        * the user's thinking toggle is on, or
        * a previous response from this provider actually carried thinking
          content (:attr:`_server_thinking_observed`) -- providers whose
          server-side thinking cannot be disabled (or ignore the toggle)
          are covered by this observation automatically, whatever the
          model is named in the user's config.

        When both are false the request runs non-thinking, and legacy
        reasoning endpoints reject the ``reasoning_content`` field, so it
        stays stripped.
        """
        return bool(getattr(self, "thinking_enabled", False)) or bool(
            getattr(self, "_server_thinking_observed", False)
        )

    def _build_request_data(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stream: bool = True,
    ) -> dict[str, Any]:
        """Build the JSON request body for the chat completions endpoint.

        Args:
            messages: Conversation history in OpenAI message format.
            tools: Optional tool definitions.
            tool_choice: Tool selection strategy.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            stream: Whether to request a streaming response.

        Returns:
            A dictionary ready to be serialised as JSON for the API request body.
        """
        # Project Encre history onto the strict OpenAI wire format at this
        # single choke point: strip internal fields, coerce content, rebuild
        # tool_calls, and forward ``reasoning_content`` when thinking is on
        # (DeepSeek/GLM/Kimi thinking rules).  Every OpenAI-protocol backend
        # goes through here (DeepSeek/Kimi/Volcengine overrides call super).
        messages, _keep = sanitize_openai_messages(
            messages, keep_reasoning=self._keep_reasoning_content
        )
        data: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max(max_tokens, 1),
            "stream": stream,
        }
        # Provider "advanced config" sampling overrides win over the loop's
        # internal defaults (some relay servers force-require these fields).
        self._merge_sampling_overrides(data)
        if stream and self._include_stream_usage:
            # Ask the provider to emit a final usage frame.  Providers that
            # reject unknown fields must keep ``_include_stream_usage`` off.
            data["stream_options"] = {"include_usage": True}
        if tools:
            data["tools"] = tools
            data["tool_choice"] = tool_choice
        # Enable thinking by default.  ``_thinking_request_param`` returns
        # the provider-native envelope -- e.g. ``{"thinking": {"type":
        # "enabled"}}`` for DeepSeek / GLM / Doubao / Xiaomi / Kimi,
        # ``{"enable_thinking": True}`` for Qwen / Hunyuan,
        # ``{"reasoning": {"enabled": True}}`` for OpenRouter, or
        # ``{"reasoning_effort": "medium"}`` for OpenAI o-series.  Returning
        # ``None`` omits the parameter (e.g. Kimi K2.7-code, non-reasoning
        # OpenAI models).  See :meth:`_thinking_request_param` for the
        # full matrix.
        thinking_param = self._thinking_request_param()
        if thinking_param:
            data.update(thinking_param)
        return data

    def _thinking_request_param(self) -> dict[str, Any] | None:
        """Return the provider-specific parameter that enables thinking.

        All backends are expected to advertise themselves as thinking-capable
        by default.  The default DeepSeek-style envelope is accepted by most
        OpenAI-protocol providers, and the per-backend overrides cover the
        few that use a different shape.

        Provider matrix
        ---------------

        * DeepSeek, GLM-4.5+, Kimi K2.5 / K2.6, Doubao, Xiaomi MiMo
          (default): ``{"thinking": {"type": "enabled"}}``
        * Alibaba Qwen QwQ / Qwen3 thinking:
          ``{"enable_thinking": True}`` (see :class:`AlibabaBackend`)
        * Tencent Hunyuan T1 / a13b:
          ``{"enable_thinking": True}`` (see :class:`TencentBackend`)
        * Kimi K2.7-code: ``None`` -- the model always thinks and the
          Moonshot API rejects any ``thinking`` parameter (see
          :class:`KimiBackend`)
        * OpenAI o-series / GPT-5.x:
          ``{"reasoning_effort": "medium"}`` (see :class:`OpenAIBackend`)
        * OpenRouter:
          ``{"reasoning": {"enabled": True}}`` (see :class:`OpenRouterBackend`)

        Anthropic and Google use native thinking protocols and do not pass
        through this method (see :class:`AnthropicBackend._build_thinking_param`
        and :class:`GoogleBackend._build_thinking_config').

        Return ``None`` to omit the parameter entirely.
        """
        return {
            "thinking": {
                "type": "enabled" if self.thinking_enabled else "disabled",
            }
        }

    # Whether to send ``stream_options: {"include_usage": true}`` on
    # streaming requests so the final usage frame is emitted.  Only
    # providers known to accept this OpenAI-spec field enable it; generic
    # OpenAI-compatible endpoints keep it off because some strict providers
    # reject unknown body fields with HTTP 400.
    _include_stream_usage: bool = False

    @staticmethod
    def _apply_prompt_caching_openai(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Split system messages at ``__PROMPT_CACHE_BOUNDARY__`` for prompt caching.

        OpenAI/DeepSeek automatically cache the message prefix when it is
        identical across requests.  By splitting a combined system message
        ``[system(static + dynamic)]`` into two system messages
        ``[system(static), system(dynamic)]``, the static prefix becomes a
        stable first message that stays identical every turn, resulting in
        automatic cache hits.  The dynamic content (memory, rules, stage)
        changes per turn in the second system message.

        Args:
            messages: The conversation history to transform.

        Returns:
            A new message list with the system message split at the boundary.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") != "system":
                result.append(msg)
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                result.append(msg)
                continue
            boundary = "__PROMPT_CACHE_BOUNDARY__"
            if boundary not in content:
                result.append(msg)
                continue
            prefix, suffix = content.split(boundary, 1)
            prefix = prefix.strip()
            suffix = suffix.strip()
            if prefix:
                result.append({"role": "system", "content": prefix})
            if suffix:
                result.append({"role": "system", "content": suffix})
        return result

    def _prepare_request_kwargs(self) -> dict[str, Any]:
        """Return additional keyword arguments for the HTTP POST request.

        Subclasses can override this to add provider-specific parameters
        (e.g. ``extra_body`` for DeepSeek's ``thinking`` parameter, or
        custom headers for Anthropic's ``anthropic-version`` header).

        Returns:
            An empty dict by default.  Subclasses should return a dict of
            kwargs to merge into the ``httpx.AsyncClient.post()`` call.
        """
        return {}

    def _extract_extra_stream_events(
        self, delta: dict[str, Any]
    ) -> list[BackendEvent]:
        """Extract reasoning/thinking text from SSE deltas.

        Checks every field name in :data:`_REASONING_FIELD_NAMES`
        (``reasoning_content``, ``reasoning``, ``thinking``, ``thought``,
        ``thoughts``, ``reasoning_text``, ``reasoning_output``) and emits
        a :class:`BackendThinking` event for the first non-empty match.
        Sub-dict shapes (``{"reasoning": {"text": "..."}}``) are also
        handled transparently.
        """
        from encre.utils.types import create_backend_thinking
        reasoning = _extract_reasoning_text(delta)
        if reasoning:
            return [create_backend_thinking(reasoning)]
        return []

    def _extract_extra_non_stream_events(
        self, message: dict[str, Any]
    ) -> list[BackendEvent]:
        """Extract reasoning/thinking text from non-stream responses.

        Same multi-field strategy as the streaming path.
        """
        from encre.utils.types import create_backend_thinking
        reasoning = _extract_reasoning_text(message)
        if reasoning:
            return [create_backend_thinking(reasoning)]
        return []

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

        When ``stream=True``, the response is consumed as an SSE stream and
        events are yielded incrementally.  When ``stream=False``, the entire
        response is fetched first, then parsed into the same event types.

        The method is decorated with :func:`retry_with_backoff` for automatic
        retry of transient HTTP errors.

        Args:
            messages: Conversation history in OpenAI message format.
            tools: Optional tool definitions.
            tool_choice: Tool selection strategy.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            stream: Whether to use SSE streaming.
            enable_caching: If True, applies prompt caching by splitting
                system messages at ``__PROMPT_CACHE_BOUNDARY__`` so the
                static prefix is automatically cached by OpenAI/DeepSeek.

        Yields:
            :class:`BackendText`, :class:`BackendToolCallDelta`,
            :class:`BackendToolCall`, :class:`BackendFinish`.
        """
        if enable_caching:
            try:
                messages = self._apply_prompt_caching_openai(messages)
            except Exception:
                logger = logging.getLogger("encre.backend")
                logger.warning("[caching] _apply_prompt_caching_openai failed, using original messages", exc_info=True)

        client = self._get_client()
        headers = self._build_headers()
        data = self._build_request_data(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
        )
        extra_kwargs = self._prepare_request_kwargs()

        # DEBUG: log request body to find 400 cause
        if stream:
            async for event in self._stream_response(client, headers, data, extra_kwargs):
                yield event
        else:
            async for event in self._non_stream_response(client, headers, data, extra_kwargs):
                yield event

    async def _stream_response(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        data: dict[str, Any],
        _extra_kwargs: dict[str, Any],
    ) -> AsyncGenerator[BackendEvent, None]:
        """Consume an SSE stream and yield parsed events.

        Uses ``httpx.AsyncClient`` with ``auth=Bearer`` and streams the
        response line by line.  Each ``data:`` line is parsed as JSON and
        converted to the appropriate :class:`BackendEvent`.

        The method handles:
        - Text deltas (``delta.content``)
        - Tool call deltas (``delta.tool_calls``) with argument buffering
        - Empty deltas (signals end of a choice)
        - ``[DONE]`` sentinel (signals end of stream)
        - Error events (``data: {"error": ...}``)
        """
        @retry_with_backoff(config=self.retry_config)
        async def _do_stream() -> AsyncGenerator[BackendEvent, None]:
            _t_req = time.time()
            _log = logging.getLogger("encre.backend")
            _msg_count = len(data.get("messages", []))
            _sys_len = len(str([m for m in data.get("messages", []) if m.get("role") == "system"]))
            _log.info("[http] POST %s/chat/completions model=%s msgs=%d sys_chars=%d thinking=%s timeout=%.0fs",
                       self.api_base_url, data.get("model", "?"), _msg_count, _sys_len,
                       data.get("thinking", {}).get("type", "disabled"), self.http_timeout)
            _log.info("[http] calling client.stream()...")
            try:
                response = await client.send(
                    client.build_request("POST", f"{self.api_base_url}/chat/completions",
                                         headers=headers, json=data),
                    stream=True,
                )
                if self.connection_monitor:
                    self.connection_monitor.record_success(self.api_base_url)
                if self.auth_manager and response.status_code not in (401, 403):
                    self.auth_manager.record_auth_success()
            except Exception as exc:
                if self.connection_monitor:
                    self.connection_monitor.record_failure(
                        self.api_base_url, format_connection_error(exc),
                    )
                _log.error("[http] client.send() raised %s: %s", type(exc).__name__, exc)
                raise
            _log.info("[http] client.send() done status=%d elapsed=%.2fs",
                       response.status_code, time.time() - _t_req)
            try:
                if response.status_code >= 400:
                    if self.auth_manager and response.status_code in (401, 403):
                        self.auth_manager.record_auth_failure()
                    body = await response.aread()
                    body_text = body.decode("utf-8", errors="replace")
                    _log.error(
                        "HTTP %s response body: %s",
                        response.status_code,
                        body_text[:2000],
                    )
                    # Raise with body text embedded in message so it survives
                    # even if the response object's body becomes unavailable
                    # after response.aclose() in the finally block.
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}: {body_text}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                # Per-request tool call buffer -- never shared across
                # concurrent chats (an instance-level dict would leak
                # partial tool calls between requests).
                tool_call_buffers: dict[int, dict[str, Any]] = {}

                _stream_usage: dict[str, Any] | None = None
                # Buffer the finish reason: usage typically arrives in the
                # final choices-less frame, so Finish is emitted only after
                # the stream has ended and usage has been captured.
                _finish_reason: str | None = None
                _t_first = time.time()
                _first_event = True

                async for line in response.aiter_lines():
                    if _first_event:
                        _log.info("[http] first SSE line after %.2fs", time.time() - _t_first)
                        _first_event = False
                    if not line.startswith("data: "):
                        continue

                    payload = line[6:].strip()

                    # End-of-stream sentinel.  Break so Finish is emitted
                    # once after the loop, with any late usage frame already
                    # captured.
                    if payload == "[DONE]":
                        break

                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    # Error event.
                    if "error" in chunk:
                        error_msg = chunk["error"].get("message", str(chunk["error"]))
                        yield BackendError(error=error_msg)
                        return

                    choices = chunk.get("choices", [])
                    if not choices:
                        # Usage may appear in a choices-less final chunk
                        if "usage" in chunk:
                            _stream_usage = normalize_usage(chunk["usage"])
                        continue

                    delta = choices[0].get("delta", {})
                    finish_reason = choices[0].get("finish_reason")

                    # Capture usage from final chunk
                    if "usage" in chunk:
                        _stream_usage = normalize_usage(chunk["usage"])

                    # Extra events (reasoning_content, etc.)
                    # Most providers put reasoning text under
                    # ``choices[0].delta.reasoning_content``.  A few put it
                    # at choice level (``choices[0].reasoning_content``) or
                    # even at chunk top level.  Try delta first, then the
                    # choice object, then the whole chunk so we catch all
                    # three shapes.
                    _extra_events = self._extract_extra_stream_events(delta)
                    if not _extra_events:
                        _extra_events = self._extract_extra_stream_events(choices[0])
                    if not _extra_events:
                        _extra_events = self._extract_extra_stream_events(chunk)
                    if _extra_events:
                        # The server is actually thinking -- remember it so
                        # follow-up requests replay ``reasoning_content``
                        # (DeepSeek/GLM/Kimi thinking rules).
                        self._server_thinking_observed = True
                    for extra in _extra_events:
                        yield extra

                    # Text delta.
                    content = delta.get("content")
                    if content:
                        yield BackendText(text=content)

                    # Tool call deltas.
                    tool_calls = delta.get("tool_calls")
                    if tool_calls:
                        for tc in tool_calls:
                            idx = tc.get("index", 0)
                            if idx not in tool_call_buffers:
                                tool_call_buffers[idx] = {
                                    "id": tc.get("id") or f"call_{idx}",
                                    "name": "",
                                    "arguments": "",
                                }
                            buf = tool_call_buffers[idx]

                            func = tc.get("function", {})
                            func_name = func.get("name", "")
                            if func_name:
                                buf["name"] += func_name

                            func_args = func.get("arguments", "")
                            if func_args:
                                buf["arguments"] += func_args

                            if func_name:
                                yield BackendToolCallDelta(
                                    index=idx, key="name", value=func_name
                                )
                            if func_args:
                                yield BackendToolCallDelta(
                                    index=idx, key="arguments", value=func_args
                                )

                    # Finish reason signals the end of this choice.  Flush
                    # buffered tool calls now, but defer Finish until the
                    # stream ends so the final usage frame is included.
                    if finish_reason:
                        _finish_reason = finish_reason
                        for tc in tool_call_buffers.values():
                            yield BackendToolCall(
                                id=tc["id"],
                                name=tc["name"],
                                arguments=tc["arguments"],
                            )
                        tool_call_buffers.clear()

                # Stream ended: flush any remaining buffered tool calls and
                # emit Finish with the usage captured from the final frame.
                for tc in tool_call_buffers.values():
                    yield BackendToolCall(
                        id=tc["id"],
                        name=tc["name"],
                        arguments=tc["arguments"],
                    )
                tool_call_buffers.clear()
                yield BackendFinish(reason=_finish_reason or "stop", usage=_stream_usage)
            finally:
                await response.aclose()

        async for event in _do_stream():
            yield event

    async def _non_stream_response(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        data: dict[str, Any],
        extra_kwargs: dict[str, Any],
    ) -> AsyncGenerator[BackendEvent, None]:
        """Send a non-streaming request and parse the single JSON response.

        The response is parsed into the same :class:`BackendEvent` types as
        the streaming path, ensuring the agent loop receives a uniform event
        stream regardless of the streaming setting.

        This method is decorated with :func:`retry_with_backoff` for automatic
        retry of transient HTTP errors.
        """
        @retry_with_backoff(config=self.retry_config)
        async def _do_non_stream() -> AsyncGenerator[BackendEvent, None]:
            try:
                response = await client.post(
                    f"{self.api_base_url}/chat/completions",
                    headers=headers,
                    json=data,
                    **extra_kwargs,
                )
                if self.connection_monitor:
                    self.connection_monitor.record_success(self.api_base_url)
                if self.auth_manager and response.status_code not in (401, 403):
                    self.auth_manager.record_auth_success()
            except Exception as exc:
                if self.connection_monitor:
                    self.connection_monitor.record_failure(
                        self.api_base_url, format_connection_error(exc),
                    )
                raise
            if self.auth_manager and response.status_code in (401, 403):
                self.auth_manager.record_auth_failure()
            response.raise_for_status()
            result = response.json()

            if "error" in result:
                error_msg = result["error"].get("message", str(result["error"]))
                yield BackendError(error=error_msg)
                return

            choices = result.get("choices", [])
            _non_stream_usage = normalize_usage(result["usage"]) if result.get("usage") else None
            if not choices:
                yield BackendFinish(reason="stop", usage=_non_stream_usage)
                return

            choice = choices[0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "stop")

            # Extra events (reasoning_content, etc.)
            # Try message first, then the choice object, then the whole
            # result to cover all three provider shapes.
            _extra_events = self._extract_extra_non_stream_events(message)
            if not _extra_events:
                _extra_events = self._extract_extra_non_stream_events(choice)
            if not _extra_events:
                _extra_events = self._extract_extra_non_stream_events(result)
            if _extra_events:
                # The server is actually thinking -- remember it so
                # follow-up requests replay ``reasoning_content``.
                self._server_thinking_observed = True
            for extra in _extra_events:
                yield extra

            # Text content.
            content = message.get("content", "")
            if content:
                yield BackendText(text=content)

            # Tool calls.
            tool_calls = message.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                yield BackendToolCall(
                    id=tc.get("id", ""),
                    name=func.get("name", ""),
                    arguments=func.get("arguments", "{}"),
                )

            yield BackendFinish(reason=finish_reason, usage=_non_stream_usage)

        async for event in _do_non_stream():
            yield event
