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
Anthropic backend -- Claude Opus 4.6/4.7, Sonnet 4.5/4.6, Haiku 4.5 (2026 lineup).

As of May 2026, Anthropic's Claude model lineup includes:

- **Claude Opus 4.6 / 4.7**: Anthropic's most capable models, excelling at
  complex reasoning, code generation, and nuanced analysis. 200K context
  window (1M in beta). Pricing: $5/$25 per 1M tokens (input/output).
  Supports thinking mode and prompt caching (90% off cache reads).

- **Claude Sonnet 4.5 / 4.6**: The balanced workhorse -- strong reasoning at
  lower cost. 200K context (1M in beta for Sonnet 4.6). Pricing: $3/$15 per
  1M tokens. Sonnet 4.6 is currently in beta with extended context support.

- **Claude Haiku 4.5**: The fastest and most cost-effective Claude model.
  200K context. Pricing: $1/$5 per 1M tokens. Ideal for high-throughput,
  latency-sensitive applications.

All Claude models support:
- Tool/function calling (native tool_use API)
- Image inputs (vision)
- Thinking/reasoning tokens (except Haiku)
- Prompt caching (90% discount on cache reads)
- Extended output (8192 tokens default)

This backend implements Anthropic's native Messages API directly (not
OpenAI-compatible), using the ``/v1/messages`` endpoint with SSE streaming.
The protocol differs significantly from OpenAI: it uses named events
(``content_block_start``, ``content_block_delta``, etc.) instead of
OpenAI's ``choices[0].delta`` structure.
"""

import json
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from encre.backends.auth import AuthManager
from encre.backends.base import BaseBackend, normalize_usage
from encre.backends.catalog import get_context_window
from encre.backends.connection import (
    ConnectionHealthMonitor,
    format_connection_error,
)
from encre.backends.retry import (
    DEFAULT_RETRY_CONFIG,
    RetryConfig,
    retry_with_backoff,
)
from encre.logging_config import get_logger
from encre.utils.types import (
    BackendEvent,
    create_backend_error,
    create_backend_finish,
    create_backend_text,
    create_backend_thinking,
    create_backend_tool_call,
    create_backend_tool_call_delta,
)

logger = get_logger("encre.backends.anthropic")


class AnthropicBackend(BaseBackend):
    """Anthropic backend for the 2026 Claude model lineup.

    Supports Claude Opus 4.6/4.7, Sonnet 4.5/4.6, and Haiku 4.5 via
    Anthropic's native Messages API.  The default model is
    ``claude-sonnet-5`` (Sonnet 5).

    This backend implements the Anthropic SSE protocol directly, handling:
    - ``content_block_start`` events for text, thinking, and tool_use blocks
    - ``content_block_delta`` events for text deltas, thinking deltas,
      signature deltas, and input_json deltas (tool call arguments)
    - ``content_block_stop`` events to finalise tool calls
    - ``message_delta`` events for finish reasons and usage metadata
    - ``error`` events for API-level errors

    Prompt caching is supported via the ``enable_caching`` parameter, which
    injects ``cache_control`` breakpoints on system messages and the last
    user message.
    """

    def __init__(
        self,
        api_key: str = "",
        model: str = "claude-sonnet-5",
        thinking_budget_tokens: int = 16000,
        thinking_mode: str = "enabled",
        thinking_effort: str = "",
        auth_manager: AuthManager | None = None,
        connection_monitor: ConnectionHealthMonitor | None = None,
        fallback_keys: list[str] | None = None,
        extra_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialise the Anthropic backend.

        Args:
            api_key: Anthropic API key.  Required for authentication via the
                ``x-api-key`` header.
            model: Claude model name.  Defaults to ``claude-sonnet-5``
                (Sonnet 5).  Other valid values: ``claude-fable-5``
                (Fable 5), ``claude-opus-4-8`` (Opus 4.8).
            thinking_budget_tokens: Token budget for extended thinking when
                ``thinking_mode == "enabled"``.  Must be smaller than
                ``max_tokens``.  Default 16,000.  Anthropic's documented
                maximum is 63,999 tokens (``MAX_THINKING_TOKENS``); the
                default 31,999 can be doubled by passing a larger value.
            thinking_mode: One of ``"enabled"`` (default, explicit budget),
                ``"adaptive"`` (Opus 4.6+ / Sonnet 4.6+ only; the model
                decides how much to think), or ``"disabled"``.  Haiku 4.5
                does not support extended thinking and the parameter is
                skipped regardless of ``thinking_mode``.
            thinking_effort: Optional effort level for ``"adaptive"`` mode.
                One of ``"low"``, ``"medium"``, ``"high"``.  Empty string
                leaves the choice to the model.  Ignored in ``"enabled"``
                and ``"disabled"`` modes.
            auth_manager: Pre-configured :class:`AuthManager` for key rotation.
                If not provided but ``fallback_keys`` is given, one is created
                automatically.
            connection_monitor: Pre-configured :class:`ConnectionHealthMonitor`.
                If not provided, a default one is created.
            fallback_keys: Additional API keys for automatic rotation on
                401/403 errors.  Ignored when ``auth_manager`` is given.
            **kwargs: Additional arguments.  Supports ``retry_config`` for
                custom :class:`RetryConfig`.
        """
        self.api_key = api_key
        self.model = model
        self.thinking_budget_tokens = thinking_budget_tokens
        self.thinking_mode = thinking_mode
        self.thinking_effort = thinking_effort
        self.retry_config: RetryConfig = kwargs.pop("retry_config", DEFAULT_RETRY_CONFIG)

        # -- Auth management --
        if auth_manager is not None:
            self.auth_manager = auth_manager
        elif fallback_keys:
            self.auth_manager = AuthManager(
                provider="anthropic",
                api_key=api_key,
                fallback_keys=fallback_keys,
            )
        else:
            self.auth_manager = None

        if self.auth_manager is not None and self.retry_config.on_auth_required is None:
            from dataclasses import replace

            self.retry_config = replace(
                self.retry_config,
                on_auth_required=self.auth_manager.refresh,
            )

        # -- Connection health monitor --
        self.connection_monitor = connection_monitor or ConnectionHealthMonitor()

        # HTTP client (x-api-key in defaults for non-chat calls; overridden
        # per-request in chat() when auth_manager is active for key rotation).
        # Custom ``extra_headers`` ride as client defaults and are merged last
        # per-request too, so user values win over the built-in ones.
        self.extra_headers: dict[str, str] = dict(extra_headers or {})
        self._client = httpx.AsyncClient(
            base_url="https://api.anthropic.com/v1",
            headers={
                "x-api-key": self.auth_manager.api_key if self.auth_manager else self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                **self.extra_headers,
            },
            timeout=httpx.Timeout(300.0, connect=30.0),
        )

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

        Implements Anthropic's Messages API with SSE streaming.  The method
        handles the full event lifecycle: content block start/delta/stop for
        text, thinking, and tool_use blocks, plus message-level deltas for
        finish reasons.

        Args:
            messages: Conversation history in OpenAI message format.  System
                messages are extracted and sent via the ``system`` parameter.
            tools: Optional tool definitions in OpenAI format.  Converted to
                Anthropic's ``tools`` parameter format.
            tool_choice: Tool selection strategy.  ``"auto"``, ``"any"``, or
                ``"none"``.  Mapped to Anthropic's ``tool_choice.type``.
            temperature: Sampling temperature (0.0 = deterministic).
            max_tokens: Maximum tokens to generate.
            stream: If True (default), uses SSE streaming.  If False, uses
                non-streaming request.
            enable_caching: If True, injects ``cache_control`` breakpoints
                for prompt caching (90% discount on cache reads).

        Yields:
            :class:`BackendText`, :class:`BackendThinking`,
            :class:`BackendToolCallDelta`, :class:`BackendToolCall`,
            :class:`BackendFinish`, or :class:`BackendError`.
        """
        if enable_caching:
            # Annotate system + last user message with cache breakpoints.
            messages = self._apply_prompt_caching(messages)

        # Cached microcompact: if the loop queued cache_edits deletions, splice
        # the cache_edits content block into the outgoing messages AT THE API
        # LAYER (the local messages stay unchanged so the cache prefix is
        # preserved).  Mirrors Claude Code's addCacheBreakpoints
        # (services/api/claude.ts:3107-3162).  cache_edits is an Anthropic
        # first-party capability; it is a no-op on non-Anthropic backends.
        _cache_edits_beta: bool = False
        if cache_edits_state is not None and enable_caching:
            try:
                from encre.cache_edits import (
                    CACHE_EDITING_BETA_HEADER,
                    attach_cache_edits,
                    consume_pending,
                    get_pinned,
                    mark_sent_to_api,
                )

                _pending = consume_pending(cache_edits_state)
                _pinned = get_pinned(cache_edits_state)
                if _pending is not None or _pinned:
                    messages = attach_cache_edits(messages, _pending, _pinned)
                    _cache_edits_beta = True
            except Exception:
                logger.debug("[cache_edits] attach failed", exc_info=True)

        # Extract system messages: the Messages API requires them in the
        # ``system`` parameter, not inside ``messages`` (which would 400).
        # Multiple system messages are merged; text blocks keep their
        # ``cache_control`` annotations so prompt caching still applies.
        system_blocks: list[dict[str, Any]] = []
        non_system_messages: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") != "system":
                non_system_messages.append(msg)
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                if content:
                    system_blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        system_blocks.append(dict(block))

        # Assemble the Anthropic Messages API request body.  The harness
        # history is OpenAI-format (tool_calls / role:"tool"), so it must be
        # projected into native blocks before it can be sent.
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._normalize_image_blocks(
                self._project_messages(non_system_messages)
            ),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        # Provider "advanced config" sampling overrides (top_k / top_p are
        # native Anthropic Messages API fields).
        self._merge_sampling_overrides(body)
        if system_blocks:
            if any("cache_control" in block for block in system_blocks):
                body["system"] = system_blocks
            else:
                merged_system = "\n\n".join(
                    block.get("text", "") for block in system_blocks
                )
                if merged_system:
                    body["system"] = merged_system
        if tools:
            body["tools"] = tools

        if tool_choice == "auto":
            body["tool_choice"] = {"type": "auto"}
        elif tool_choice == "any":
            body["tool_choice"] = {"type": "any"}
        elif tool_choice == "none":
            body["tool_choice"] = {"type": "none"}

        thinking_param = self._build_thinking_param(max_tokens)
        if thinking_param is not None:
            # Enable extended thinking when the model/config supports it.
            body["thinking"] = thinking_param

        try:

            async def _make_request() -> httpx.Response:
                # Build per-request headers so the active (possibly rotated)
                # API key from the auth manager is always used.
                _req_headers: dict[str, str] = {}
                if self.auth_manager is not None:
                    _req_headers["x-api-key"] = self.auth_manager.api_key
                elif self.api_key:
                    _req_headers["x-api-key"] = self.api_key
                # cache_edits requires the context-management beta header
                # (latched on once enabled, matching Claude Code's
                # cache-editing header latch).  Only sent when we actually
                # attached a cache_edits block this request.
                if _cache_edits_beta:
                    _req_headers["anthropic-beta"] = CACHE_EDITING_BETA_HEADER
                if self.extra_headers:
                    _req_headers.update(self.extra_headers)

                _url = str(self._client.base_url)
                try:
                    resp = await self._client.send(
                        self._client.build_request(
                            "POST",
                            "/messages",
                            json=body,
                            headers=_req_headers,
                        ),
                        stream=True,
                    )
                    if self.connection_monitor:
                        self.connection_monitor.record_success(_url)
                    if self.auth_manager and resp.status_code not in (401, 403):
                        self.auth_manager.record_auth_success()
                    return resp
                except Exception as exc:
                    if self.connection_monitor:
                        self.connection_monitor.record_failure(
                            _url,
                            format_connection_error(exc),
                        )
                    raise

            _retry_decorator = retry_with_backoff(config=self.retry_config)
            _retried_request = _retry_decorator(_make_request)
            resp = await _retried_request()

            async with resp:
                if resp.status_code != 200:
                    if self.auth_manager and resp.status_code in (401, 403):
                        self.auth_manager.record_auth_failure()
                    error_body = await resp.aread()
                    error_text = error_body.decode(errors="replace")
                    msg = f"Anthropic API error {resp.status_code}: {error_text}"
                    logger.error(msg)
                    yield create_backend_error(msg)
                    return

                current_tool_use: dict[str, Any] | None = None
                current_tool_index: int = 0
                finish_reason: str = "stop"
                _input_tokens: int = 0
                _output_tokens: int = 0
                _cache_deleted: int = 0
                _cache_read: int = 0
                _cache_creation: int = 0

                # Anthropic SSE emits paired lines: "event: <type>" followed
                # by "data: <json>". Parse them into BackendEvent objects.
                async for line in resp.aiter_lines():
                    if not line.startswith("event: "):
                        continue
                    event_type = line[7:].strip()
                    data_line = await resp.__anext__()
                    if not data_line.startswith("data: "):
                        continue
                    data = json.loads(data_line[6:].strip())

                    if event_type == "message_start":
                        msg = data.get("message", {})
                        msg_usage = msg.get("usage", {})
                        _input_tokens = msg_usage.get("input_tokens", 0)
                        # Anthropic reports prompt-cache hits separately:
                        # cache_read_input_tokens = tokens served from cache,
                        # cache_creation_input_tokens = tokens written to
                        # cache this request (billed at a write premium).
                        _cache_read = msg_usage.get("cache_read_input_tokens", 0) or 0
                        _cache_creation = msg_usage.get("cache_creation_input_tokens", 0) or 0
                        # Mark every registered tool_result as "sent to API" so
                        # the next turn's deletion candidates are eligible.
                        # This is safe to do here because message_start fires
                        # exactly once per request, after the API accepted it.
                        if cache_edits_state is not None:
                            try:
                                from encre.cache_edits import mark_sent_to_api

                                mark_sent_to_api(cache_edits_state)
                            except Exception:
                                logger.debug("exception suppressed", exc_info=True)
                    elif event_type == "content_block_start":
                        # A new content block begins (text, thinking, tool_use).
                        block = data.get("content_block", {})
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                yield create_backend_text(text)
                        elif block.get("type") == "thinking":
                            thinking_text = block.get("thinking", "")
                            if thinking_text:
                                yield create_backend_thinking(thinking_text)
                        elif block.get("type") == "redacted_thinking":
                            yield create_backend_thinking("[Thinking redacted]")
                        elif block.get("type") == "tool_use":
                            current_tool_use = {
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "arguments": "",
                            }
                            current_tool_index = data.get("index", 0)
                            yield create_backend_tool_call_delta(current_tool_index, "name", block.get("name", ""))

                    elif event_type == "content_block_delta":
                        # Incremental update to the current block.
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                yield create_backend_text(text)
                        elif delta.get("type") == "thinking_delta":
                            thinking_text = delta.get("thinking", "")
                            if thinking_text:
                                yield create_backend_thinking(thinking_text)
                        elif delta.get("type") == "signature_delta":
                            sig = delta.get("signature", "")
                            if sig:
                                yield create_backend_thinking("", signature_delta=sig)
                        elif delta.get("type") == "input_json_delta":
                            partial = delta.get("partial_json", "")
                            if current_tool_use is not None:
                                current_tool_use["arguments"] += partial
                            yield create_backend_tool_call_delta(data.get("index", 0), "arguments", partial)

                    elif event_type == "content_block_stop":
                        # Block finished; flush any pending tool call.
                        if current_tool_use is not None:
                            yield create_backend_tool_call(
                                id=current_tool_use["id"],
                                name=current_tool_use["name"],
                                arguments=current_tool_use["arguments"],
                            )
                            current_tool_use = None

                    elif event_type == "message_delta":
                        # Message-level update carrying the stop reason and usage.
                        delta = data.get("delta", {})
                        stop_reason = delta.get("stop_reason", "")
                        # Map Anthropic stop reasons to our finish reasons.
                        if stop_reason == "end_turn":
                            finish_reason = "stop"
                        elif stop_reason == "tool_use":
                            finish_reason = "tool_calls"
                        elif stop_reason == "max_tokens":
                            finish_reason = "max_tokens"
                        else:
                            finish_reason = stop_reason or "stop"
                        msg_usage = data.get("usage", {})
                        _output_tokens = msg_usage.get("output_tokens", 0)
                        _cache_deleted = msg_usage.get("cache_deleted_input_tokens", 0)

                    elif event_type == "error":
                        error_data = data.get("error", {})
                        err_msg = error_data.get("message", str(data))
                        logger.error(f"Anthropic stream error: {err_msg}")
                        yield create_backend_error(err_msg)

                _usage = None
                if _input_tokens or _output_tokens or _cache_read or _cache_creation:
                    _raw_usage: dict[str, Any] = {
                        "input_tokens": _input_tokens,
                        "output_tokens": _output_tokens,
                    }
                    if _cache_read:
                        _raw_usage["cache_read_input_tokens"] = _cache_read
                    if _cache_creation:
                        _raw_usage["cache_creation_input_tokens"] = _cache_creation
                    if _cache_deleted:
                        _raw_usage["cache_deleted_input_tokens"] = _cache_deleted
                    _usage = normalize_usage(_raw_usage)
                yield create_backend_finish(finish_reason, usage=_usage)

        except Exception as e:
            logger.error(f"Anthropic backend request failed: {e}", extra={"model": self.model})
            yield create_backend_error(str(e))

    def supports_tool_calling(self) -> bool:
        """All Claude models support native tool calling via the tool_use API."""
        return True

    def _build_thinking_param(self, max_tokens: int) -> dict[str, Any] | None:
        """Return the Anthropic extended-thinking parameter, or ``None``.

        Anthropic's Messages API supports three extended-thinking modes:

        * ``"adaptive"``: the model decides how much to think.  Only
          available on Opus 4.6+ / Sonnet 4.6+ / Claude 5.  Returns
          ``{"type": "adaptive"}`` or ``{"type": "adaptive",
          "effort": "low|medium|high"}`` when an effort level is set.
        * ``"enabled"``: explicit token budget.  Returns
          ``{"type": "enabled", "budget_tokens": N}`` where ``N`` is
          clamped to fit under ``max_tokens - 1``.
        * ``"disabled"``: returns ``None`` (omit the parameter).

        Haiku 4.5 does not support extended thinking and the parameter
        is always skipped for that model family.

        Anthropic's documented budget range is 1,024..31,999 tokens,
        with a doubled maximum (63,999) available via the
        ``MAX_THINKING_TOKENS`` beta header.  We clamp to ``max_tokens -
        1024`` to ensure there is always enough room for the final
        answer.
        """
        m = (self.model or "").lower()
        # Haiku does not support extended thinking at all.
        if "haiku" in m:
            return None
        if self.thinking_mode == "disabled":
            return None
        # Adaptive mode is only valid for Opus 4.6+ / Sonnet 4.6+ / Claude 5.
        if self.thinking_mode == "adaptive" and ("opus-4-6" in m or "opus-4-7" in m or "sonnet-4-6" in m or "claude-5" in m):
            effort = (self.thinking_effort or "").lower()
            if effort in {"low", "medium", "high"}:
                return {"type": "adaptive", "effort": effort}
            return {"type": "adaptive"}
        # Explicit budget mode (default).
        budget = int(self.thinking_budget_tokens or 16000)
        # Clamp to the documented maximum and leave room for the answer.
        budget = max(1024, min(budget, min(max_tokens - 1024, 63999)))
        return {"type": "enabled", "budget_tokens": budget}

    def context_window_size(self) -> int:
        """Return the context window size for Claude models.

        The model catalog (:func:`encre.backends.catalog.get_context_window`)
        is the single source of truth; the family heuristics below only run
        for names the catalog does not know.

        2026 reference:
            - Claude Opus 5 / Fable 5 / Sonnet 5: 1,000,000 tokens
            - Claude Opus 4.8 / Sonnet 4.6: 1,000,000 tokens
            - Claude Opus 4.6 / Sonnet 4.5: 200,000 tokens
            - Claude Haiku 4.5 / 4.0: 200,000 tokens
        """
        catalog_window = get_context_window(self.model)
        if catalog_window is not None:
            return catalog_window
        model_lower = self.model.lower()
        # 5th generation -- 1M context.
        if "opus-5" in model_lower or "fable-5" in model_lower or "sonnet-5" in model_lower:
            return 1_000_000
        # 4.8 / 4.7 / 4.6 (Sonnet) -- 1M context.
        if "opus-4-8" in model_lower or "opus-4-7" in model_lower or "sonnet-4-6" in model_lower:
            return 1_000_000
        return 200_000

    async def aclose(self) -> None:
        """Close the HTTP client session."""
        await self._client.aclose()

    def supports_thinking(self) -> bool:
        """Claude Opus and Sonnet support thinking tokens; Haiku does not."""
        return True

    def supports_prompt_caching(self) -> bool:
        """All Claude models support prompt caching at 90% off cache reads."""
        return True

    def count_tokens(self, text: str) -> int:
        """Estimate token count using tiktoken or char/4 heuristic.

        Anthropic uses a BPE tokenizer similar to GPT.  For precise
        counts, use the Anthropic API ``/v1/messages/count_tokens``
        endpoint (requires an async call).
        """
        if not text:
            return 0
        try:
            from encre.utils.tokens import estimate_tokens

            return estimate_tokens(text, model="claude-sonnet-5")
        except Exception:
            return len(text) // 4

    async def list_models(self) -> list[str]:
        """Fetch available models from Anthropic's models endpoint.

        Returns a list of model IDs available to the API key.
        Results are cached for 5 minutes.
        """
        import time

        now = time.time()
        cache_key = f"anthropic:{self.api_key[:8] if self.api_key else 'noauth'}"
        if (
            hasattr(self, "_models_cache")
            and hasattr(self, "_models_cache_ts")
            and cache_key == getattr(self, "_models_cache_key", "")
            and now - self._models_cache_ts < 300
        ):
            return self._models_cache  # type: ignore[attr-defined]

        try:
            resp = await self._client.get("/models")
            resp.raise_for_status()
            data = resp.json()
            models: list[str] = []
            for item in data.get("data", []):
                model_id = item.get("id", "")
                if model_id:
                    models.append(model_id)
            models.sort()
        except Exception:
            models = []

        self._models_cache = models
        self._models_cache_ts = now
        self._models_cache_key = cache_key
        return models

    @staticmethod
    def _project_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Project the harness's OpenAI-format history into Anthropic blocks.

        The Messages API has no ``tool_calls`` / ``role: "tool"`` message
        shapes: assistant tool invocations must arrive as ``tool_use``
        content blocks and tool results as ``tool_result`` blocks inside a
        ``user`` message directly after them.  Thinking blocks are replayed
        with the server-issued signatures stored in ``reasoning_blocks`` --
        replaying thinking text alone (or with a stale signature) is a hard
        400, so a block without a usable signature is omitted.

        Consecutive ``tool`` results are merged into one ``user`` turn and
        flushed before any following message, matching the API's strict
        block ordering.  ``image_url`` blocks pass through untouched for
        :meth:`_normalize_image_blocks` to convert afterwards.
        """
        out: list[dict[str, Any]] = []
        pending_tool_results: list[dict[str, Any]] = []

        def _flush_tool_results() -> None:
            if pending_tool_results:
                out.append({"role": "user", "content": list(pending_tool_results)})
                pending_tool_results.clear()

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "tool":
                result_content = (
                    content
                    if isinstance(content, str)
                    else json.dumps(content or "", ensure_ascii=False)
                )
                # An empty tool_use_id is a hard 400 on the Messages API;
                # tombstone/recovery paths can synthesize tool results
                # before a real id exists, so placeholder ids keep the
                # request sendable (and the pair visibly broken instead
                # of crashing the whole turn).
                result_block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id") or "toolu_missing_id",
                    "content": result_content,
                }
                if msg.get("is_error"):
                    result_block["is_error"] = True
                pending_tool_results.append(result_block)
                continue

            # A tool_result must directly follow its tool_use inside a user
            # turn -- flush before any other message shape lands.
            _flush_tool_results()

            if role == "assistant":
                blocks: list[dict[str, Any]] = []
                reasoning_blocks = msg.get("reasoning_blocks")
                if not reasoning_blocks:
                    reasoning_text = msg.get("reasoning_content", "")
                    reasoning_blocks = (
                        [{"text": reasoning_text, "signature": ""}] if reasoning_text else []
                    )
                for rb in reasoning_blocks:
                    if not isinstance(rb, dict):
                        continue
                    sig = rb.get("signature") or ""
                    if sig:
                        blocks.append(
                            {
                                "type": "thinking",
                                "thinking": rb.get("text") or "",
                                "signature": sig,
                            }
                        )
                text = (
                    content
                    if isinstance(content, str)
                    else "".join(
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    )
                )
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in msg.get("tool_calls") or []:
                    func = tc.get("function") or {}
                    args = func.get("arguments", "")
                    if isinstance(args, str):
                        try:
                            args_obj: Any = json.loads(args) if args else {}
                        except ValueError:
                            args_obj = {"_raw": args}
                    else:
                        args_obj = args or {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id") or f"toolu_missing_{len(blocks)}",
                            "name": func.get("name", ""),
                            "input": args_obj,
                        }
                    )
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
                continue

            # user (and any other role): normalize the content into blocks.
            if isinstance(content, str):
                norm: list[dict[str, Any]] = [{"type": "text", "text": content}] if content else []
            elif isinstance(content, list):
                norm = [item if isinstance(item, dict) else {"type": "text", "text": str(item)} for item in content]
            else:
                norm = [{"type": "text", "text": json.dumps(content) if content else ""}]
            if norm:
                out.append({"role": "user", "content": norm})

        _flush_tool_results()
        return out

    @staticmethod
    def _normalize_image_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-style ``image_url`` blocks into native image blocks.

        The Messages API expects ``{"type": "image", "source": {"type":
        "base64", ...}}`` while the harness's lingua franca (and what the
        computer tool's screenshot projection emits) is the OpenAI-style
        ``{"type": "image_url", "image_url": {"url": "data:..."}}`` block.
        Data URLs are rewritten to native source blocks; http(s) URLs are
        rewritten to URL sources; any other block passes through untouched.
        Non-mutating: messages without image blocks are returned as-is.
        """
        changed = False
        out: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")
            if not (isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "image_url" for b in content
            )):
                out.append(msg)
                continue
            blocks: list[dict[str, Any]] = []
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "image_url"):
                    blocks.append(block)
                    continue
                url = (block.get("image_url") or {}).get("url", "")
                if isinstance(url, str) and url.startswith("data:"):
                    header, _, data = url.partition(",")
                    media_type = header[5:].split(";", 1)[0] or "image/png"
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    })
                elif isinstance(url, str) and url.startswith(("http://", "https://")):
                    blocks.append({
                        "type": "image",
                        "source": {"type": "url", "url": url},
                    })
                else:
                    blocks.append(block)
            new_msg = dict(msg)
            new_msg["content"] = blocks
            out.append(new_msg)
            changed = True
        return out if changed else messages

    @staticmethod
    def _apply_prompt_caching(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Inject ``cache_control`` breakpoints for Anthropic prompt caching.

        Caches system messages and the last user message.  ``cache_control`` is
        only valid on ``text`` and ``tool_result`` content blocks -- images and
        other block types must be skipped.  This method walks content blocks in
        reverse to find the *last textual* block rather than blindly marking
        whatever happens to appear last in the list.

        Args:
            messages: The conversation history to annotate with cache breakpoints.

        Returns:
            A new message list with ``cache_control`` annotations added to
            system messages and the last user message's final text block.
        """
        # Valid block types for cache_control per Anthropic API docs.
        _cacheable_block_types = frozenset({"text", "tool_result"})

        result: list[dict[str, Any]] = []
        system_indices: list[int] = []
        last_user_idx: int | None = None

        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                system_indices.append(i)
            elif msg.get("role") == "user":
                last_user_idx = i

        for i, msg in enumerate(messages):
            msg_copy = dict(msg)
            should_cache = i in system_indices or i == last_user_idx

            if should_cache:
                content = msg_copy.get("content")
                if isinstance(content, str):
                    msg_copy["content"] = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
                elif isinstance(content, list):
                    blocks: list[dict[str, Any]] = []
                    cacheable_last_idx: int | None = None
                    for rev_j in range(len(content) - 1, -1, -1):
                        block_type = content[rev_j].get("type", "")
                        if block_type in _cacheable_block_types:
                            cacheable_last_idx = rev_j
                            break

                    for j, block in enumerate(content):
                        block_copy = dict(block)
                        if j == cacheable_last_idx:
                            block_copy["cache_control"] = {"type": "ephemeral"}
                        blocks.append(block_copy)
                    msg_copy["content"] = blocks

            result.append(msg_copy)

        return result

    # ── Multimodal capability declarations ───────────────────────────────
    # Anthropic natively supports: files (Files API), batch (Messages
    # Batches) and vision input (via chat).  Other modalities
    # (image generation, TTS, embeddings, moderation, fine-tuning,
    # Realtime, Responses) are not available in the Anthropic API and
    # therefore raise ``NotImplementedError`` if called.

    def supports_files(self) -> bool:
        return True

    def supports_batch(self) -> bool:
        return True

    def supports_vision_input(self) -> bool:
        return True

    # ── Files ────────────────────────────────────────────────────────────

    async def upload_file(
        self,
        filename: str,
        content_b64: str,
        purpose: str = "",
        mime_type: str = "application/octet-stream",
        extra_params: dict[str, Any] | None = None,
    ):
        """Upload a file to Anthropic via the ``/v1/files`` endpoint.

        Anthropic files are purpose-agnostic (the ``purpose`` field is
        optional and currently informational).  Files are required for
        document/image inputs to the Messages API when the content is
        too large to inline as base64.
        """
        import base64

        from encre.utils.types import FileObject

        content = base64.b64decode(content_b64)
        files = {"file": (filename, content, mime_type)}
        data: dict[str, Any] = {}
        if purpose:
            data["purpose"] = purpose
        if extra_params:
            data.update(extra_params)
        response = await self._client.post("/files", files=files, data=data)
        response.raise_for_status()
        payload = response.json() if response.content else {}
        return FileObject(
            id=str(payload.get("id", "")),
            object=str(payload.get("type", "file")),
            bytes=int(payload.get("size_bytes", 0)),
            created_at=int(payload.get("created_at", 0)),
            filename=str(payload.get("filename", filename)),
            mime_type=str(payload.get("mime_type", mime_type)),
            provider="anthropic",
            metadata={
                k: v
                for k, v in payload.items()
                if k
                not in {
                    "id",
                    "type",
                    "size_bytes",
                    "created_at",
                    "filename",
                    "mime_type",
                }
            },
        )

    async def list_files(
        self,
        _purpose: str | None = None,
        limit: int = 100,
        after: str | None = None,
        order: str = "desc",
        extra_params: dict[str, Any] | None = None,
    ):
        """List files via the Anthropic ``/v1/files`` endpoint."""
        from encre.utils.types import FileListResponse, FileObject

        params: dict[str, Any] = {"limit": min(int(limit), 1000), "order": order}
        if after:
            params["after_id"] = after
        if extra_params:
            params.update(extra_params)
        response = await self._client.get("/files", params=params)
        response.raise_for_status()
        payload = response.json() if response.content else {}
        items = payload.get("data", []) or []
        files: list[FileObject] = []
        for item in items:
            files.append(
                FileObject(
                    id=str(item.get("id", "")),
                    object=str(item.get("type", "file")),
                    bytes=int(item.get("size_bytes", 0)),
                    created_at=int(item.get("created_at", 0)),
                    filename=str(item.get("filename", "")),
                    mime_type=str(item.get("mime_type", "")),
                    provider="anthropic",
                    metadata={
                        k: v
                        for k, v in item.items()
                        if k
                        not in {
                            "id",
                            "type",
                            "size_bytes",
                            "created_at",
                            "filename",
                            "mime_type",
                        }
                    },
                )
            )
        has_more = bool(payload.get("has_more", payload.get("last_id")))
        return FileListResponse(
            object="list",
            data=files,
            has_more=has_more,
        )

    async def retrieve_file(
        self,
        file_id: str,
    ):
        """Fetch metadata for a single uploaded file."""
        from encre.utils.types import FileObject

        response = await self._client.get(f"/files/{file_id}")
        response.raise_for_status()
        payload = response.json() if response.content else {}
        return FileObject(
            id=str(payload.get("id", file_id)),
            object=str(payload.get("type", "file")),
            bytes=int(payload.get("size_bytes", 0)),
            created_at=int(payload.get("created_at", 0)),
            filename=str(payload.get("filename", "")),
            mime_type=str(payload.get("mime_type", "")),
            provider="anthropic",
            metadata={
                k: v
                for k, v in payload.items()
                if k
                not in {
                    "id",
                    "type",
                    "size_bytes",
                    "created_at",
                    "filename",
                    "mime_type",
                }
            },
        )

    async def delete_file(
        self,
        file_id: str,
    ) -> bool:
        """Delete an Anthropic file by id."""
        response = await self._client.delete(f"/files/{file_id}")
        return not response.status_code >= 400

    async def download_file(
        self,
        file_id: str,
    ):
        """Download the raw content of an Anthropic file."""
        import base64

        from encre.utils.types import ProviderFileContent

        response = await self._client.get(f"/files/{file_id}/content")
        response.raise_for_status()
        content_b64 = base64.b64encode(response.content).decode("ascii")
        return ProviderFileContent(
            file_id=file_id,
            filename="",
            content_b64=content_b64,
            mime_type=response.headers.get("content-type", ""),
            provider="anthropic",
        )

    # ── Messages Batches ─────────────────────────────────────────────────

    async def create_batch(
        self,
        requests,
        _endpoint: str = "/v1/messages",
        _completion_window: str = "24h",
        metadata: dict[str, Any] | None = None,
        extra_params: dict[str, Any] | None = None,
    ):
        """Create an Anthropic Messages Batch.

        The list of ``BatchRequest`` items is serialised to a JSONL blob
        and uploaded via the Files API; the resulting file id is then
        submitted to ``/v1/messages/batches``.
        """
        import base64
        import json as _json

        if not requests:
            raise ValueError("create_batch requires at least one request")

        # Stage the JSONL payload.
        lines = []
        for r in requests:
            lines.append(
                _json.dumps(
                    {
                        "custom_id": r.custom_id,
                        "params": r.body,
                    },
                    ensure_ascii=False,
                )
            )
        jsonl_b64 = base64.b64encode(("\n".join(lines)).encode("utf-8")).decode("ascii")
        file_obj = await self.upload_file(
            filename="batch_input.jsonl",
            content_b64=jsonl_b64,
            mime_type="application/jsonl",
        )

        payload: dict[str, Any] = {"input_file_id": file_obj.id}
        if metadata:
            payload["metadata"] = metadata
        if extra_params:
            payload.update(extra_params)

        response = await self._client.post("/messages/batches", json=payload)
        response.raise_for_status()
        data = response.json() if response.content else {}
        return _parse_anthropic_batch(data, provider="anthropic")

    async def retrieve_batch(
        self,
        batch_id: str,
    ):
        """Fetch the current state of an Anthropic Messages Batch."""
        response = await self._client.get(f"/messages/batches/{batch_id}")
        response.raise_for_status()
        data = response.json() if response.content else {}
        return _parse_anthropic_batch(data, provider="anthropic")

    async def list_batches(
        self,
        limit: int = 20,
        after: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ):
        """List recent Anthropic Messages Batches."""
        from encre.utils.types import BatchListResponse

        params: dict[str, Any] = {"limit": min(int(limit), 1000)}
        if after:
            params["after_id"] = after
        if extra_params:
            params.update(extra_params)
        response = await self._client.get("/messages/batches", params=params)
        response.raise_for_status()
        data = response.json() if response.content else {}
        items = data.get("data", []) or []
        batches = [_parse_anthropic_batch(item, provider="anthropic") for item in items]
        return BatchListResponse(
            object="list",
            data=batches,
            has_more=bool(data.get("has_more", data.get("last_id"))),
        )

    async def cancel_batch(
        self,
        batch_id: str,
    ):
        """Cancel an in-flight Anthropic Messages Batch."""
        response = await self._client.post(f"/messages/batches/{batch_id}/cancel")
        response.raise_for_status()
        data = response.json() if response.content else {}
        return _parse_anthropic_batch(data, provider="anthropic")


def _parse_anthropic_batch(data: dict[str, Any], provider: str):
    """Convert an Anthropic Messages Batch payload into our shape."""
    from encre.utils.types import BatchObject

    counts = data.get("request_counts", {}) or {}
    if not isinstance(counts, dict):
        counts = {}
    return BatchObject(
        id=str(data.get("id", "")),
        object="batch",
        status=str(data.get("processing_status", data.get("status", ""))),
        endpoint="/v1/messages",
        input_file_id=str(data.get("input_file_id", "")),
        output_file_id=str(data.get("output_file_id", "")),
        error_file_id=str(data.get("error_file_id", "")),
        created_at=int(
            data.get("created_at", "").timestamp() if hasattr(data.get("created_at"), "timestamp") else data.get("created_at", 0) or 0
        ),
        expires_at=int(
            data.get("expires_at", "").timestamp() if hasattr(data.get("expires_at"), "timestamp") else data.get("expires_at", 0) or 0
        ),
        completed_at=None,
        failed_at=None,
        request_counts={str(k): int(v) for k, v in counts.items() if isinstance(v, int | float)},
        completion_window="24h",
        provider=provider,
        metadata={
            k: v
            for k, v in data.items()
            if k
            not in {
                "id",
                "processing_status",
                "status",
                "input_file_id",
                "output_file_id",
                "error_file_id",
                "created_at",
                "expires_at",
                "request_counts",
            }
        },
    )
