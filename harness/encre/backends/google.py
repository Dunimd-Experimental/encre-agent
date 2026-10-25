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
Google backend -- Gemini 2.5 Pro, Gemini 2.5 Flash (2026 lineup).

As of May 2026, Google's Gemini model lineup includes:

- **Gemini 2.5 Pro**: Google's most capable model with 1M token context
  window, multimodal support (text, image, video, audio), and thinking mode.
  Pricing: $1.25/$10 per 1M tokens (short context <=200K), $2.50/$15 per 1M
  tokens (long context >200K).

- **Gemini 2.5 Flash**: The fast, cost-effective variant with 1M context and
  similar multimodal capabilities.  Pricing: $0.15/$0.60 per 1M tokens.

Both models support:
- Tool/function calling (via Google's functionCall/functionResponse protocol)
- Thinking/reasoning tokens
- Multimodal inputs (text, images, video, audio)
- Google Search grounding (optional, via ``enable_grounding``)
- Streaming and non-streaming responses

This backend implements Google's Generative Language API directly (not
OpenAI-compatible), using the ``streamGenerateContent`` and
``generateContent`` endpoints.  The protocol uses a different message format
than OpenAI: roles are ``user``/``model``/``function``, and tool calls use
``functionCall``/``functionResponse`` blocks instead of OpenAI's
``tool_calls`` array.

Retry logic via :func:`retry_with_backoff` handles transient HTTP errors
(429, 502, 503, 504) and network timeouts for both streaming and non-streaming
requests.
"""

import asyncio
import json
import logging
import random
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from encre.backends.base import BaseBackend, normalize_usage
from encre.utils.types import (
    BackendEvent,
    create_backend_error,
    create_backend_finish,
    create_backend_text,
    create_backend_thinking,
    create_backend_tool_call,
    create_backend_tool_call_delta,
)

logger = logging.getLogger("encre.backends.google")


class GoogleBackend(BaseBackend):
    """Google backend for the 2026 Gemini model lineup.

    Supports Gemini 2.5 Pro (default) and Gemini 2.5 Flash via Google's
    Generative Language API at ``https://generativelanguage.googleapis.com/v1beta``.

    This backend handles the full protocol conversion between OpenAI's message
    format and Google's format, including:
    - Role mapping: ``user`` -> ``user``, ``assistant`` -> ``model``,
      ``tool`` -> ``function``, ``system`` -> ``systemInstruction``
    - Tool conversion: OpenAI ``function`` tools -> Google ``functionDeclaration``
    - Content block conversion: text, image_url, and image_data blocks
    - Tool call buffering for streaming responses
    - Finish reason mapping (STOP, MAX_TOKENS, SAFETY, etc.)

    Optional Google Search grounding can be enabled via the ``enable_grounding``
    parameter, which adds a ``googleSearch`` tool to the request.
    """

    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "gemini-3.7-flash",
        enable_grounding: bool = False,
        enable_thinking: bool = True,
        thinking_budget: int = -1,
        thinking_level: str = "",
        **_kwargs: Any,
    ) -> None:
        """Initialise the Google backend.

        Args:
            api_key: Google AI Studio API key.  Required for authentication
                via the ``key`` query parameter.
            base_url: Custom API base URL.  Defaults to
                ``https://generativelanguage.googleapis.com/v1beta``.
            model: Model name.  Defaults to ``gemini-2.5-pro``.  Other valid
                values: ``gemini-2.5-flash``, ``gemini-3.0-pro``,
                ``gemini-3.0-flash``.
            enable_grounding: If True, enables Google Search grounding for
                real-time information retrieval.
            enable_thinking: If True (default), enables the model's native
                thinking mode.  Gemini 2.5 emits a ``thought`` part in the
                response; Gemini 3.0 emits ``reasoning_text`` blocks.  Both
                are extracted as :class:`BackendThinking` events.
            thinking_budget: Token budget for the thinking phase on
                **Gemini 2.5** models.  ``-1`` (default) lets the model
                decide dynamically; ``0`` disables thinking; any positive
                integer is the explicit budget.  Ignored on Gemini 3.0+.
            thinking_level: Effort level for the thinking phase on
                **Gemini 3.0** models.  One of ``"minimal"``, ``"low"``,
                ``"medium"``, ``"high"``.  Empty string (default) leaves
                the choice to the model.  Ignored on Gemini 2.5.
            **_kwargs: Additional arguments (currently unused).
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") or self.DEFAULT_BASE_URL
        self.model = model
        self.enable_grounding = enable_grounding
        self.enable_thinking = enable_thinking
        self.thinking_budget = thinking_budget
        self.thinking_level = thinking_level
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=30.0),
        )

    def _convert_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Convert OpenAI-format messages to Google Generative Language format.

        Handles role mapping, content block conversion, and system instruction
        extraction.  Tool call messages from the assistant are converted to
        ``functionCall`` blocks, and tool response messages are converted to
        ``functionResponse`` blocks.

        Args:
            messages: OpenAI-format message list.

        Returns:
            A tuple of (contents, system_instruction) where ``contents`` is the
            converted message list and ``system_instruction`` is the extracted
            system prompt (or None).
        """
        contents: list[dict[str, Any]] = []
        system_texts: list[str] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                # Collect every system message; multiple system messages are
                # merged so none is silently dropped.
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = "\n".join(
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    )
                else:
                    text = json.dumps(content) if content else ""
                if text:
                    system_texts.append(text)
                continue

            parts: list[dict[str, Any]] = []

            if isinstance(content, str):
                parts.append({"text": content})
            elif isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        parts.append({"text": item.get("text", "")})
                    elif item.get("type") == "image_url":
                        image_url = item.get("image_url", {}).get("url", "")
                        if image_url:
                            parts.append(
                                {
                                    "inline_data": {
                                        "mime_type": "image/jpeg",
                                        "data": image_url,
                                    },
                                }
                            )
                    elif item.get("type") == "image" or item.get("type") == "image_data":
                        parts.append(
                            {
                                "inline_data": {
                                    "mime_type": item.get("mime_type", "image/jpeg"),
                                    "data": item.get("data", ""),
                                }
                            }
                        )
            elif isinstance(content, dict):
                parts.append({"text": json.dumps(content)})

            if role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        func_args = func.get("arguments", "")
                        parts.append(
                            {
                                "functionCall": {
                                    "name": func.get("name", ""),
                                    "args": (json.loads(func_args) if func_args else {}),
                                },
                            }
                        )
                else:
                    mapped_role = "model"
                    contents.append({"role": mapped_role, "parts": parts})
                    continue

            elif role == "tool":
                tool_name = msg.get("name", "")
                resp_content = content if isinstance(content, str) else json.dumps(content)
                mapped_content: list[dict[str, Any]] = [
                    {
                        "functionResponse": {
                            "name": tool_name,
                            "response": {"content": resp_content},
                        }
                    }
                ]
                mapped_role = "function"
                contents.append({"role": mapped_role, "parts": mapped_content})
                continue

            if role == "user":
                mapped_role = "user"
            elif role == "assistant":
                mapped_role = "model"
            else:
                mapped_role = "user"

            contents.append({"role": mapped_role, "parts": parts})

        system_instruction: dict[str, Any] | None = (
            {"parts": [{"text": "\n\n".join(system_texts)}]} if system_texts else None
        )
        return contents, system_instruction

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-format tools to Google function declarations.

        Args:
            tools: OpenAI-format tool list (``[{"type": "function", "function": {...}}]``).

        Returns:
            A list of Google-format tool declarations with ``functionDeclarations``.
        """
        function_declarations: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                declaration: dict[str, Any] = {
                    "name": func.get("name", ""),
                }
                if func.get("description"):
                    declaration["description"] = func["description"]
                if func.get("parameters"):
                    declaration["parameters"] = func["parameters"]
                function_declarations.append(declaration)
        return [{"functionDeclarations": function_declarations}]

    def _build_thinking_config(self) -> dict[str, Any] | None:
        """Return the Gemini thinking config, or ``None`` when disabled.

        Gemini 2.5 uses ``thinking_budget`` (integer).  Gemini 3.0 uses
        ``thinking_level`` (string enum ``"minimal"|"low"|"medium"|"high"``).
        The two keys are mutually exclusive; the API rejects requests
        that include both.

        When ``enable_thinking`` is False, returns ``{"thinking_budget": 0}``
        to explicitly turn off thinking on 2.5 models.
        """
        if not self.enable_thinking:
            # 2.5 model + budget 0 = thinking disabled.
            if not self._is_gemini_3():
                return {"thinking_budget": 0}
            return None
        if self._is_gemini_3():
            # Gemini 3.0+: thinking_level controls effort.
            # The Gemini API expects uppercase values (LOW / HIGH).
            if self.thinking_level:
                level = self.thinking_level.upper()
                if level in {"LOW", "HIGH"}:
                    return {"thinking_level": level}
            return None  # let the model decide
        # Gemini 2.5: thinking_budget (integer or -1 for dynamic).
        budget = int(self.thinking_budget)
        if budget < -1:
            budget = -1
        return {"thinking_budget": budget}

    def _is_gemini_3(self) -> bool:
        """Return True if the configured model is Gemini 3.0+."""
        m = (self.model or "").lower()
        return "gemini-3" in m or "gemini-3.0" in m

    def _build_body(
        self,
        contents: list[dict[str, Any]],
        system_instruction: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        tool_choice: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Build the request body for Gemini API calls."""
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        # Provider "advanced config" sampling overrides -- Gemini uses
        # camelCase generationConfig keys.
        self._merge_sampling_overrides(
            generation_config, mapping={"top_p": "topP", "top_k": "topK"},
        )

        if tool_choice == "any":
            generation_config["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
        elif tool_choice == "none":
            generation_config["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        elif tool_choice == "auto":
            generation_config["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        thinking_config = self._build_thinking_config()
        if thinking_config is not None:
            generation_config["thinkingConfig"] = thinking_config

        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }

        if system_instruction:
            body["systemInstruction"] = system_instruction

        if tools:
            body["tools"] = self._convert_tools(tools)

        if self.enable_grounding:
            body["tools"] = [*body.get("tools", []), {"googleSearch": {}}]

        return body

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

        Implements Google's Generative Language API with SSE streaming for
        ``streamGenerateContent`` and non-streaming for ``generateContent``.
        Both paths use exponential backoff retry for transient errors.

        Args:
            messages: Conversation history in OpenAI message format.
            tools: Optional tool definitions in OpenAI format.
            tool_choice: Tool selection strategy (``"auto"``, ``"any"``, ``"none"``).
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            stream: If True (default), uses SSE streaming.
            enable_caching: Not yet supported by Google's API (ignored).

        Yields:
            :class:`BackendText`, :class:`BackendToolCallDelta`,
            :class:`BackendToolCall`, :class:`BackendFinish`, or
            :class:`BackendError`.
        """
        contents, system_instruction = self._convert_messages(messages)
        body = self._build_body(
            contents,
            system_instruction,
            tools,
            tool_choice,
            temperature,
            max_tokens,
        )

        endpoint = f"/models/{self.model}:streamGenerateContent" if stream else f"/models/{self.model}:generateContent"
        url = f"{self.base_url}{endpoint}?key={self.api_key}&alt=sse" if stream else f"{self.base_url}{endpoint}?key={self.api_key}"

        try:
            if stream:
                async for event in self._stream_with_retry(url, body):
                    yield event
            else:
                async for event in self._non_stream_with_retry(url, body):
                    yield event
        except Exception as e:
            yield create_backend_error(str(e))

    async def _stream_with_retry(self, url: str, body: dict[str, Any]) -> AsyncGenerator[BackendEvent, None]:
        """Stream response with exponential backoff retry.

        Retries on 429/502/503/504 status codes, timeouts, and connection
        errors.  On retry the entire stream is re-requested from scratch.
        """
        max_retries = 5
        rate_limit_retries = 8
        base_delay = 1.0
        max_delay = 60.0

        for attempt in range(max(rate_limit_retries, max_retries) + 1):
            try:
                async for event in self._do_stream(url, body):
                    yield event
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {429, 502, 503, 504}:
                    raise
                if exc.response.status_code == 429 and attempt >= rate_limit_retries:
                    yield create_backend_error("Gemini rate limit exhausted")
                    return
                if exc.response.status_code != 429 and attempt >= max_retries:
                    yield create_backend_error("Gemini server error retries exhausted")
                    return
            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.TransportError):
                if attempt >= max_retries:
                    yield create_backend_error("Gemini network error retries exhausted")
                    return

            delay = min(base_delay * (2**attempt), max_delay)
            await asyncio.sleep(random.uniform(0, delay))

    async def _non_stream_with_retry(self, url: str, body: dict[str, Any]) -> AsyncGenerator[BackendEvent, None]:
        """Non-streaming response with exponential backoff retry."""
        max_retries = 5
        rate_limit_retries = 8
        base_delay = 1.0
        max_delay = 60.0

        for attempt in range(max(rate_limit_retries, max_retries) + 1):
            try:
                async for event in self._do_non_stream(url, body):
                    yield event
                return
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {429, 502, 503, 504}:
                    raise
                if exc.response.status_code == 429 and attempt >= rate_limit_retries:
                    yield create_backend_error("Gemini rate limit exhausted")
                    return
                if exc.response.status_code != 429 and attempt >= max_retries:
                    yield create_backend_error("Gemini server error retries exhausted")
                    return
            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.TransportError):
                if attempt >= max_retries:
                    yield create_backend_error("Gemini network error retries exhausted")
                    return

            delay = min(base_delay * (2**attempt), max_delay)
            await asyncio.sleep(random.uniform(0, delay))

    async def _do_stream(self, url: str, body: dict[str, Any]) -> AsyncGenerator[BackendEvent, None]:
        """Execute a single streaming request to Gemini API."""
        async with self._client.stream("POST", url, json=body) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                error_text = error_body.decode(errors="replace")
                logger.error("Gemini API error %s: %s", resp.status_code, error_text)
                raise httpx.HTTPStatusError(
                    f"Gemini API error {resp.status_code}: {error_text}",
                    request=resp.request,
                    response=resp,
                )

            tool_call_buffers: dict[int, dict[str, Any]] = {}
            current_idx = 0
            finish_reason: str = "stop"
            accumulated_text: dict[int, str] = {}
            _usage_metadata: dict[str, Any] | None = None

            # Gemini streams SSE "data: {json}" lines; parse each candidate.
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if not payload:
                    continue

                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    # Ignore malformed/partial JSON lines defensively.
                    continue

                candidates = chunk.get("candidates", [])
                if not candidates:
                    if "usageMetadata" in chunk:
                        _usage_metadata = chunk["usageMetadata"]
                    continue

                candidate = candidates[0]
                content = candidate.get("content", {})
                parts = content.get("parts", [])

                for part_idx, part in enumerate(parts):
                    # Gemini thinking content can appear in several shapes:
                    # 2.5: {"thought": "..."}      -- a top-level ``thought`` key
                    # 3.0: {"text": "...", "thought": true}  -- a text part flagged as thought
                    # 3.0: {"thoughtSignature": "..."} + plain text follows in next part
                    # 3.0 alt: {"reasoning_text": "..."}
                    if part.get("thought") is True and isinstance(part.get("text"), str):
                        th = part.get("text", "")
                        if th:
                            yield create_backend_thinking(th)
                    elif "thought" in part and isinstance(part.get("thought"), str):
                        th = part.get("thought", "")
                        if th:
                            yield create_backend_thinking(th)
                    elif "reasoning_text" in part:
                        th = part.get("reasoning_text", "")
                        if th:
                            yield create_backend_thinking(th)
                    elif "text" in part:
                        text = part.get("text", "")
                        if text:
                            # Gemini may resend cumulative text; emit only the
                            # newly-appended suffix as an incremental delta.
                            if part_idx not in accumulated_text:
                                accumulated_text[part_idx] = ""
                            prev = accumulated_text[part_idx]
                            new_part = text[len(prev) :] if text.startswith(prev) else text
                            accumulated_text[part_idx] = text
                            if new_part:
                                yield create_backend_text(new_part)
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        buf_idx = current_idx
                        current_idx += 1
                        name = fc.get("name", "")
                        args = fc.get("args", {})
                        args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                        tool_call_buffers[buf_idx] = {
                            "id": f"call_{buf_idx}",
                            "name": name,
                            "arguments": args_str,
                        }
                        yield create_backend_tool_call_delta(buf_idx, "name", name)
                        yield create_backend_tool_call_delta(buf_idx, "arguments", args_str)

                finish = candidate.get("finishReason")
                if finish:
                    finish_reason = self._map_finish_reason(finish)

                if "usageMetadata" in chunk:
                    _usage_metadata = chunk["usageMetadata"]
                elif candidate.get("usageMetadata"):
                    _usage_metadata = candidate["usageMetadata"]

            for idx in sorted(tool_call_buffers.keys()):
                buf = tool_call_buffers[idx]
                yield create_backend_tool_call(
                    id=buf["id"],
                    name=buf["name"],
                    arguments=buf["arguments"],
                )

            _usage = None
            if _usage_metadata:
                # Gemini implicit caching: cachedContentTokenCount is the
                # portion of promptTokenCount served from cache.
                _usage = normalize_usage({
                    "input_tokens": _usage_metadata.get("promptTokenCount", 0),
                    "output_tokens": _usage_metadata.get("candidatesTokenCount", 0),
                    "cachedContentTokenCount": _usage_metadata.get("cachedContentTokenCount", 0) or 0,
                })
            yield create_backend_finish(finish_reason, usage=_usage)

    async def _do_non_stream(self, url: str, body: dict[str, Any]) -> AsyncGenerator[BackendEvent, None]:
        """Execute a single non-streaming request to Gemini API."""
        resp = await self._client.post(url, json=body)
        resp.raise_for_status()

        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            prompt_feedback = data.get("promptFeedback", {})
            block_reason = prompt_feedback.get("blockReason", "unknown")
            yield create_backend_error(f"Gemini blocked: {block_reason}")
            return

        candidate = candidates[0]
        finish_reason = candidate.get("finishReason", "STOP")
        mapped_reason = self._map_finish_reason(finish_reason)

        content = candidate.get("content", {})
        parts = content.get("parts", [])

        tool_call_buffers: dict[int, dict[str, Any]] = {}
        current_idx = 0

        for part in parts:
            # Gemini 2.5 emits {"thought": "..."} or
            # {"text": "...", "thought": true}; Gemini 3.0 uses
            # {"reasoning_text": "..."} or thought-flagged text parts.
            if part.get("thought") is True and isinstance(part.get("text"), str):
                yield create_backend_thinking(part["text"])
            elif isinstance(part.get("thought"), str) and part["thought"]:
                yield create_backend_thinking(part["thought"])
            elif isinstance(part.get("reasoning_text"), str) and part["reasoning_text"]:
                yield create_backend_thinking(part["reasoning_text"])
            elif "text" in part:
                yield create_backend_text(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                buf_idx = current_idx
                current_idx += 1
                name = fc.get("name", "")
                args = fc.get("args", {})
                args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                tool_call_buffers[buf_idx] = {
                    "id": f"call_{buf_idx}",
                    "name": name,
                    "arguments": args_str,
                }
                yield create_backend_tool_call_delta(buf_idx, "name", name)
                yield create_backend_tool_call_delta(buf_idx, "arguments", args_str)

        for idx in sorted(tool_call_buffers.keys()):
            buf = tool_call_buffers[idx]
            yield create_backend_tool_call(
                id=buf["id"],
                name=buf["name"],
                arguments=buf["arguments"],
            )

        usage_meta = data.get("usageMetadata", {})
        _usage = None
        if usage_meta:
            # Gemini implicit caching: cachedContentTokenCount is the
            # portion of promptTokenCount served from cache.
            _usage = normalize_usage({
                "input_tokens": usage_meta.get("promptTokenCount", 0),
                "output_tokens": usage_meta.get("candidatesTokenCount", 0),
                "cachedContentTokenCount": usage_meta.get("cachedContentTokenCount", 0) or 0,
            })
        yield create_backend_finish(mapped_reason, usage=_usage)

    def _map_finish_reason(self, reason: str) -> str:
        """Map Google finish reasons to unified finish reasons.

        Google uses uppercase finish reasons (STOP, MAX_TOKENS, SAFETY, etc.)
        which are mapped to the unified format used by the agent loop.

        Args:
            reason: Google's finish reason string.

        Returns:
            A unified finish reason string (``"stop"``, ``"max_tokens"``, ``"error"``).
        """
        mapping = {
            "STOP": "stop",
            "MAX_TOKENS": "max_tokens",
            "SAFETY": "error",
            "RECITATION": "error",
            "MALFORMED_FUNCTION_CALL": "error",
            "OTHER": "stop",
        }
        return mapping.get(reason, "stop")

    def supports_tool_calling(self) -> bool:
        """Gemini models support function calling via functionDeclarations."""
        return True

    def context_window_size(self) -> int:
        """Return the context window size for Gemini models.

        Both Gemini 2.5 Pro and 2.5 Flash support 1,048,576 (1M) token
        context windows.
        """
        return 1048576

    def count_tokens(self, text: str) -> int:
        """Estimate token count using tiktoken or char/4 heuristic.

        Uses Gemini-compatible token estimation.  For precise counts,
        use the Cloud AI API ``countTokens`` endpoint.
        """
        if not text:
            return 0
        try:
            from encre.utils.tokens import estimate_tokens

            return estimate_tokens(text, model="gemini-pro")
        except Exception:
            return len(text) // 4

    async def list_models(self) -> list[str]:
        """Fetch available models from Google's models endpoint.

        Returns a list of model names available to the API key.
        Results are cached for 5 minutes.
        """
        import time

        now = time.time()
        cache_key = f"google:{self.api_key[:8] if self.api_key else 'noauth'}"
        if (
            hasattr(self, "_models_cache")
            and hasattr(self, "_models_cache_ts")
            and cache_key == getattr(self, "_models_cache_key", "")
            and now - self._models_cache_ts < 300
        ):
            return self._models_cache  # type: ignore[attr-defined]

        try:
            url = f"{self.base_url}/models?key={self.api_key}"
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            models: list[str] = []
            for item in data.get("models", []):
                name = item.get("name", "")
                if name:
                    name = name.replace("models/", "")
                    models.append(name)
            models.sort()
        except Exception:
            models = []

        self._models_cache = models
        self._models_cache_ts = now
        self._models_cache_key = cache_key
        return models

    async def aclose(self) -> None:
        """Close the HTTP client session."""
        await self._client.aclose()

    def supports_thinking(self) -> bool:
        """Gemini 2.5 models support thinking/reasoning tokens."""
        return True

    # ── Multimodal capability declarations ───────────────────────────────
    # The Google Generative Language API exposes embeddings, files,
    # batches and supervised fine-tuning on Gemini models.  Image
    # generation, TTS and STT are only available via Vertex AI and
    # therefore not implemented here.  Realtime audio is exposed via
    # the Gemini Live API (``/v1beta/models/.../live`` WebSocket).

    def supports_embeddings(self) -> bool:
        return True

    def supports_files(self) -> bool:
        return True

    def supports_batch(self) -> bool:
        return True

    def supports_fine_tuning(self) -> bool:
        return True

    def supports_realtime(self) -> bool:
        return True

    def supports_vision_input(self) -> bool:
        return True

    # ── Embeddings ───────────────────────────────────────────────────────

    async def create_embeddings(
        self,
        input: str | list[str],
        model: str | None = None,
        _encoding_format: str = "float",
        dimensions: int | None = None,
        _user: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ):
        """Generate embedding vectors via ``models/{model}:batchEmbedContents``."""
        from encre.utils.types import EmbeddingResponse, EmbeddingResult

        if isinstance(input, str):
            inputs = [{"content": {"parts": [{"text": input}]}}]
        else:
            inputs = [{"content": {"parts": [{"text": x}]}} for x in input]

        if dimensions is not None:
            for item in inputs:
                item["outputDimensionality"] = dimensions  # type: ignore[assignment]

        payload: dict[str, Any] = {"requests": inputs}
        if extra_params:
            payload.update(extra_params)

        model_name = model or "text-embedding-005"
        url = f"{self.base_url}/models/{model_name}:batchEmbedContents"
        response = await self._client.post(url, params={"key": self.api_key}, json=payload)
        response.raise_for_status()
        data = response.json() if response.content else {}
        embeddings_raw = data.get("embeddings", []) or []

        results: list[EmbeddingResult] = []
        for idx, item in enumerate(embeddings_raw):
            values = item.get("values", []) if isinstance(item, dict) else []
            stats = item.get("statistics", {}) if isinstance(item, dict) else {}
            results.append(
                EmbeddingResult(
                    index=int(item.get("index", idx)) if isinstance(item, dict) else idx,
                    embedding=list(values),
                    object="embedding",
                    model=model_name,
                )
            )
            _ = stats  # currently unused; reserved for future use

        return EmbeddingResponse(
            object="list",
            data=results,
            model=model_name,
            provider="google",
            usage={},
        )

    # ── Files ────────────────────────────────────────────────────────────

    async def upload_file(
        self,
        filename: str,
        content_b64: str,
        purpose: str = "",
        mime_type: str = "application/octet-stream",
        extra_params: dict[str, Any] | None = None,
    ):
        """Upload a file via the Gemini Files API (resumable upload)."""
        import base64

        from encre.utils.types import FileObject

        content = base64.b64decode(content_b64)
        num_bytes = len(content)

        # Step 1: initiate resumable upload.
        start_url = f"{self.base_url}/upload/v1beta/files"
        start_headers = {
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(num_bytes),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "Content-Type": "application/json",
        }
        start_body: dict[str, Any] = {"file": {"display_name": filename}}
        if extra_params:
            start_body["file"].update(extra_params)

        resp = await self._client.post(
            start_url,
            params={"key": self.api_key},
            headers=start_headers,
            json=start_body,
        )
        resp.raise_for_status()
        upload_url = resp.headers.get("X-Goog-Upload-URL")
        if not upload_url:
            raise RuntimeError("Google Files API did not return an X-Goog-Upload-URL header")

        # Step 2: upload the bytes.
        upload_headers = {
            "Content-Length": str(num_bytes),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
        }
        resp = await self._client.post(upload_url, headers=upload_headers, content=content)
        resp.raise_for_status()
        payload = resp.json() if resp.content else {}
        file_obj = payload.get("file", payload) if isinstance(payload, dict) else {}

        return FileObject(
            id=str(file_obj.get("name", "")),
            object="file",
            bytes=int(file_obj.get("size_bytes", num_bytes)),
            created_at=0,
            filename=str(file_obj.get("display_name", filename)),
            purpose=purpose,
            mime_type=mime_type,
            provider="google",
            metadata={
                k: v
                for k, v in file_obj.items()
                if k
                not in {
                    "name",
                    "size_bytes",
                    "display_name",
                    "mime_type",
                }
            }
            if isinstance(file_obj, dict)
            else {},
        )

    async def list_files(
        self,
        _purpose: str | None = None,
        limit: int = 100,
        _after: str | None = None,
        _order: str = "desc",
        extra_params: dict[str, Any] | None = None,
    ):
        """List files via the Gemini Files API."""
        from encre.utils.types import FileListResponse, FileObject

        params: dict[str, Any] = {"pageSize": min(int(limit), 100)}
        if extra_params:
            params.update(extra_params)
        url = f"{self.base_url}/files"
        response = await self._client.get(url, params={"key": self.api_key, **params})
        response.raise_for_status()
        data = response.json() if response.content else {}
        items = data.get("files", []) or []
        files: list[FileObject] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            files.append(
                FileObject(
                    id=str(item.get("name", "")),
                    object="file",
                    bytes=int(item.get("size_bytes", 0)),
                    created_at=0,
                    filename=str(item.get("display_name", "")),
                    mime_type=str(item.get("mime_type", "")),
                    provider="google",
                    metadata={
                        k: v
                        for k, v in item.items()
                        if k
                        not in {
                            "name",
                            "size_bytes",
                            "display_name",
                            "mime_type",
                        }
                    },
                )
            )
        return FileListResponse(object="list", data=files, has_more=False)

    async def retrieve_file(
        self,
        file_id: str,
    ):
        """Fetch a file's metadata."""
        from encre.utils.types import FileObject

        url = f"{self.base_url}/{file_id}"
        response = await self._client.get(url, params={"key": self.api_key})
        response.raise_for_status()
        data = response.json() if response.content else {}
        return FileObject(
            id=str(data.get("name", file_id)),
            object="file",
            bytes=int(data.get("size_bytes", 0)),
            created_at=0,
            filename=str(data.get("display_name", "")),
            mime_type=str(data.get("mime_type", "")),
            provider="google",
            metadata={
                k: v
                for k, v in data.items()
                if k
                not in {
                    "name",
                    "size_bytes",
                    "display_name",
                    "mime_type",
                }
            }
            if isinstance(data, dict)
            else {},
        )

    async def delete_file(
        self,
        file_id: str,
    ) -> bool:
        """Delete an uploaded file by id."""
        url = f"{self.base_url}/{file_id}"
        response = await self._client.delete(url, params={"key": self.api_key})
        return not response.status_code >= 400

    # ── Batch ────────────────────────────────────────────────────────────

    async def create_batch(
        self,
        requests,
        endpoint: str = "/v1/models/{model}:generateContent",
        completion_window: str = "24h",
        metadata: dict[str, Any] | None = None,
        extra_params: dict[str, Any] | None = None,
    ):
        """Create a Gemini batch via ``batches`` endpoint."""
        from encre.utils.types import BatchObject

        batch_requests: list[dict[str, Any]] = []
        for r in requests:
            batch_requests.append(
                {
                    "request": r.body,
                    "metadata": {"custom_id": r.custom_id},
                }
            )

        model_name = self.model or "gemini-2.5-pro"
        # Resolve a placeholder in the endpoint URL.
        resolved_endpoint = endpoint.format(model=model_name)
        payload: dict[str, Any] = {
            "batch": {
                "display_name": f"batch-{int(__import__('time').time())}",
                "input_config": {
                    "requests": {
                        "requests": batch_requests,
                    },
                },
            }
        }
        if metadata:
            payload["batch"]["metadata"] = metadata
        if extra_params:
            payload["batch"].update(extra_params)

        url = f"{self.base_url}/models/{model_name}:batchGenerateContent"
        response = await self._client.post(url, params={"key": self.api_key}, json=payload)
        response.raise_for_status()
        data = response.json() if response.content else {}
        metadata_block = data.get("metadata", {}) if isinstance(data, dict) else {}
        return BatchObject(
            id=str(metadata_block.get("name", "")),
            object="batch",
            status=str(metadata_block.get("state", "")),
            endpoint=resolved_endpoint,
            input_file_id="",
            completion_window=completion_window,
            created_at=0,
            expires_at=0,
            request_counts={},
            output_file_id="",
            error_file_id="",
            provider="google",
            metadata={
                k: v
                for k, v in metadata_block.items()
                if k
                not in {
                    "name",
                    "state",
                }
            }
            if isinstance(metadata_block, dict)
            else {},
        )

    # ── Fine-tuning (tuning) ─────────────────────────────────────────────

    async def create_fine_tuning_job(
        self,
        training_file: str,
        model: str,
        hyperparameters=None,
        validation_file: str | None = None,
        suffix: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ):
        """Create a Gemini supervised fine-tuning job."""
        from encre.utils.types import FineTuneJob

        tuned_model_name = f"tunedModels/{suffix or 'encre-tuned'}-{int(__import__('time').time())}"
        payload: dict[str, Any] = {
            "baseModel": f"models/{model}",
            "tunedModelDisplayName": suffix or "encre-tuned-model",
            "trainingDataUri": training_file,
            "tuningSpec": {
                "hyperparameters": {},
            },
        }
        if hyperparameters is not None:
            hparams: dict[str, Any] = {}
            if hyperparameters.n_epochs not in (None, "auto"):
                hparams["epochCount"] = int(hyperparameters.n_epochs)
            if hyperparameters.batch_size not in (None, "auto"):
                hparams["batchSize"] = int(hyperparameters.batch_size)
            if hyperparameters.learning_rate_multiplier not in (None, "auto"):
                hparams["learningRate"] = float(hyperparameters.learning_rate_multiplier)
            payload["tuningSpec"]["hyperparameters"] = hparams  # type: ignore[assignment]
        if validation_file:
            payload["validationDataUri"] = validation_file
        if extra_params:
            payload.update(extra_params)

        url = f"{self.base_url}/tunedModels"
        response = await self._client.post(url, params={"key": self.api_key}, json=payload)
        response.raise_for_status()
        data = response.json() if response.content else {}
        return FineTuneJob(
            id=str(data.get("name", tuned_model_name)),
            object="fine_tuning.job",
            model=str(data.get("baseModel", model)),
            created_at=0,
            fine_tuned_model=str(data.get("name", tuned_model_name)),
            status=str(data.get("state", "")),
            training_file=training_file,
            validation_file=validation_file or "",
            hyperparameters={},
            trained_tokens=None,
            error={},
            provider="google",
        )

    # ── Realtime (Gemini Live) ───────────────────────────────────────────

    async def create_realtime_session(
        self,
        config=None,
        extra_params: dict[str, Any] | None = None,
    ):
        """Open a Gemini Live realtime session.

        Gemini Live uses a WebSocket protocol at
        ``wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent``.
        This implementation returns a session descriptor whose ``transport``
        is the websocket if ``websockets`` (or ``httpx_ws``) is installed;
        otherwise the caller must open the websocket themselves.
        """
        from encre.utils.types import RealtimeSession

        cfg = config
        if cfg is None:
            from encre.utils.types import RealtimeSessionConfig

            cfg = RealtimeSessionConfig(model=self.model)

        model_name = cfg.model or self.model or "gemini-2.5-flash"
        transport = await self._open_gemini_live_transport(model_name, cfg, extra_params)
        return RealtimeSession(
            session_id="",
            model=model_name,
            expires_at=0,
            transport=transport,
            provider="google",
        )

    async def _open_gemini_live_transport(self, _model_name, _cfg, _extra_params):
        ws_url = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
        from httpx_ws import aconnect_ws  # type: ignore[import-not-found]

        try:
            return await aconnect_ws(
                ws_url,
                params={"key": self.api_key},
                headers={"Content-Type": "application/json"},
            )
        except Exception:
            return None

    async def close_realtime_session(
        self,
        session,
    ) -> None:
        transport = session.transport
        if transport is None:
            return
        close = getattr(transport, "aclose", None) or getattr(transport, "close", None)
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result

    def supports_grounding(self) -> bool:
        """Gemini models support Google Search grounding."""
        return True
