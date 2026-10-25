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
AWS Bedrock backend -- Converse API (Claude, Llama, Mistral, etc.).

AWS Bedrock is a managed service that provides access to foundation models
from Anthropic, Meta, Mistral, AI21, Cohere, and Amazon via a single API.  # noqa: E402
This backend uses the Bedrock Converse API, which provides a unified
interface for all supported models.

Supported model families (2026):
- **Anthropic Claude**: Opus 4.6/4.7, Sonnet 4.5/4.6, Haiku 4.5
- **Meta Llama**: Llama 3.3 70B, Llama 4 Scout
- **Mistral**: Mistral Large 2, Mistral Small
- **Amazon**: Nova Pro, Nova Lite
- **AI21**: Jamba 1.5
- **Cohere**: Command R+

Key characteristics:
- Requires AWS credentials (access key + secret key + region)
- Uses the ``aws-sdk`` (``boto3``) for authentication and API calls
- Supports streaming via the Converse Stream API
- Tool calling is supported for Claude and Llama models
- Context window varies by model (4K-200K)
- Pricing varies by model and region

This backend implements the Bedrock Converse API directly using ``boto3``,
not the OpenAI-compatible endpoint.  The Converse API provides a unified
interface that normalises model responses across providers.
"""

import json
from collections.abc import AsyncGenerator
from typing import Any

from encre.backends.base import BaseBackend
from encre.utils.types import (
    BackendEvent,
    create_backend_error,
    create_backend_finish,
    create_backend_text,
    create_backend_thinking,
    create_backend_tool_call,
    create_backend_tool_call_delta,
)


class BedrockBackend(BaseBackend):
    """AWS Bedrock backend using the Converse API.

    Provides access to foundation models from Anthropic, Meta, Mistral,
    Amazon, AI21, and Cohere via AWS Bedrock's unified Converse API.

    Authentication is handled via AWS credentials (``aws_access_key_id``,
    ``aws_secret_access_key``, ``region_name``).  If not provided, the
    default AWS credential chain is used (environment variables, ~/.aws/credentials).

    Args:
        model: Bedrock model ID (e.g., ``"anthropic.claude-sonnet-5"``).
            Defaults to ``"anthropic.claude-sonnet-5"``.
        aws_access_key_id: Optional AWS access key ID.
        aws_secret_access_key: Optional AWS secret access key.
        region_name: AWS region name (e.g., ``"us-west-2"``).  Defaults to
            ``"us-east-1"``.
        **kwargs: Additional arguments (currently unused).
    """

    def __init__(
        self,
        model: str = "anthropic.claude-sonnet-5",
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
        region_name: str = "us-east-1",
        thinking_mode: str = "enabled",
        thinking_budget_tokens: int = 16000,
        thinking_effort: str = "",
        **_kwargs: Any,
    ) -> None:
        """Initialise the Bedrock backend.

        Args:
            model: Bedrock model ID.  Defaults to
                ``anthropic.claude-sonnet-5``.
            aws_access_key_id: Optional AWS access key ID.
            aws_secret_access_key: Optional AWS secret access key.
            region_name: AWS region.  Defaults to ``us-east-1``.
            thinking_mode: One of ``"enabled"`` (explicit budget),
                ``"adaptive"`` (Claude Opus 4.6+ / Sonnet 4.6+ only), or
                ``"disabled"``.  Only applies to Anthropic Claude models.
            thinking_budget_tokens: Token budget for extended thinking on
                Claude models.  Ignored when ``thinking_mode`` is
                ``"disabled"`` or ``"adaptive"``.
            thinking_effort: Optional effort level for ``"adaptive"`` mode.
                One of ``"low"``, ``"medium"``, ``"high"``.  Empty string
                leaves the choice to the model.  Only applies to Anthropic
                Claude models.
            **_kwargs: Additional arguments (currently unused).
        """
        self.model = model
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key
        self.region_name = region_name
        self.thinking_mode = thinking_mode
        self.thinking_budget_tokens = thinking_budget_tokens
        self.thinking_effort = thinking_effort
        self._client = None

    async def _ensure_client(self) -> None:
        """Lazy-initialise the Bedrock runtime client.

        Creates a ``boto3`` Bedrock Runtime client with the configured
        credentials.  If no credentials are provided, the default AWS
        credential chain is used.

        The synchronous boto3 session creation is run in a thread pool
        executor to avoid blocking the async event loop.

        Raises:
            ImportError: If ``boto3`` is not installed.
        """
        if self._client is not None:
            return
        import asyncio

        def _create_client():
            try:
                import boto3

                session_kwargs: dict[str, Any] = {"region_name": self.region_name}
                if self.aws_access_key_id and self.aws_secret_access_key:
                    session_kwargs["aws_access_key_id"] = self.aws_access_key_id
                    session_kwargs["aws_secret_access_key"] = self.aws_secret_access_key
                session = boto3.Session(**session_kwargs)
                return session.client("bedrock-runtime")
            except ImportError as e:
                raise ImportError("boto3 not installed. Install with: pip install boto3") from e

        loop = asyncio.get_running_loop()
        self._client = await loop.run_in_executor(None, _create_client)

    def _convert_messages(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Convert OpenAI-format messages to Bedrock Converse API format.

        Handles role mapping (``system`` -> ``system``, ``assistant`` ->
        ``assistant``, ``user`` -> ``user``, ``tool`` -> ``user`` with
        ``toolResult`` content blocks).

        Args:
            messages: OpenAI-format message list.

        Returns:
            A tuple of (converted_messages, system_prompt) where
            ``system_prompt`` is the merged system prompt (or None).
        """
        converted: list[dict[str, Any]] = []
        system_texts: list[str] = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                # Collect every system message; multiple system messages are
                # merged so none is silently dropped.
                if isinstance(content, str):
                    if content:
                        system_texts.append(content)
                elif isinstance(content, list):
                    text = "\n".join(
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    )
                    if text:
                        system_texts.append(text)
                elif content:
                    system_texts.append(json.dumps(content))
                continue

            if role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    content_blocks: list[dict[str, Any]] = []
                    # Bedrock extended-thinking models require the
                    # assistant's ``reasoningContent`` (with its signature)
                    # to be replayed on tool-call turns; text alone is
                    # rejected, so a signature-less block is omitted.
                    reasoning_blocks = msg.get("reasoning_blocks")
                    if not reasoning_blocks:
                        reasoning_text = msg.get("reasoning_content", "")
                        reasoning_blocks = (
                            [{"text": reasoning_text, "signature": ""}] if reasoning_text else []
                        )
                    for rb in reasoning_blocks:
                        if not isinstance(rb, dict):
                            continue
                        if rb.get("signature"):
                            content_blocks.append(
                                {
                                    "reasoningContent": {
                                        "text": rb.get("text") or "",
                                        "signature": rb["signature"],
                                    }
                                }
                            )
                    for tc in tool_calls:
                        func = tc.get("function", {})
                        func_args = func.get("arguments", "{}")
                        content_blocks.append(
                            {
                                "toolUse": {
                                    "toolUseId": tc.get("id", f"tooluse_{hash(str(tc))}"),
                                    "name": func.get("name", ""),
                                    "input": (json.loads(func_args) if func_args else {}),
                                }
                            }
                        )
                    converted.append({"role": "assistant", "content": content_blocks})
                else:
                    converted.append({"role": "assistant", "content": [{"text": content}]})
                continue

            if role == "tool":
                tool_use_id = msg.get("tool_call_id", f"tooluse_{hash(str(msg))}")
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "toolResult": {
                                    "toolUseId": tool_use_id,
                                    "content": [
                                        {
                                            "text": (content if isinstance(content, str) else json.dumps(content)),
                                        }
                                    ],
                                }
                            }
                        ],
                    }
                )
                continue

            if isinstance(content, str):
                converted.append({"role": "user", "content": [{"text": content}]})
            elif isinstance(content, list):
                blocks = []
                for item in content:
                    if item.get("type") == "text":
                        blocks.append({"text": item.get("text", "")})
                    elif item.get("type") == "image_url":
                        blocks.append(self._convert_image_block(item))
                converted.append({"role": "user", "content": blocks})

        system_message: str | None = "\n\n".join(system_texts) if system_texts else None
        return converted, system_message

    @staticmethod
    def _convert_image_block(item: dict[str, Any]) -> dict[str, Any]:
        """Convert an OpenAI-format image block to Bedrock image format.

        Handles both base64 data URLs (``data:image/jpeg;base64,...``)
        and raw URL references.  For data URLs, the base64 payload is
        decoded into raw bytes.  For external URLs, the raw bytes are
        fetched or passed through.

        Args:
            item: An OpenAI content block with ``type: "image_url"``.

        Returns:
            A Bedrock-format image block with ``image.format`` and
            ``image.source.bytes``.
        """
        import base64

        image_url = item.get("image_url", {})
        url = image_url.get("url", "")
        mime_type = "image/jpeg"

        if url.startswith("data:"):
            header, b64_data = url.split(",", 1)
            mime_type = header.split(";")[0].replace("data:", "")
            image_bytes = base64.b64decode(b64_data)
        else:
            image_bytes = url.encode("utf-8")

        fmt = mime_type.split("/")[-1] if "/" in mime_type else "jpeg"
        return {
            "image": {
                "format": fmt,
                "source": {"bytes": image_bytes},
            }
        }

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert OpenAI-format tools to Bedrock Converse API tool format.

        Args:
            tools: OpenAI-format tool list.

        Returns:
            A list of Bedrock-format tool specifications.
        """
        converted: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                tool_spec: dict[str, Any] = {
                    "toolSpec": {
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                    }
                }
                if func.get("parameters"):
                    tool_spec["toolSpec"]["inputSchema"] = {"json": func["parameters"]}
                converted.append(tool_spec)
        return converted

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

        Uses the Bedrock Converse API (streaming or non-streaming) to
        generate responses from the configured model.

        Args:
            messages: Conversation history in OpenAI message format.
            tools: Optional tool definitions in OpenAI format.
            tool_choice: Tool selection strategy (``"auto"``, ``"any"``, ``"none"``).
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.
            stream: If True (default), uses the Converse Stream API.
            enable_caching: Not supported by Bedrock Converse API (ignored).

        Yields:
            :class:`BackendText`, :class:`BackendToolCallDelta`,
            :class:`BackendToolCall`, :class:`BackendFinish`, or
            :class:`BackendError`.
        """
        try:
            await self._ensure_client()
        except ImportError as e:
            yield create_backend_error(str(e))
            return

        import asyncio

        # Translate the OpenAI-style history into Converse API shape and
        # split out any system prompt (Converse takes it separately).
        converted_messages, system_message = self._convert_messages(messages)

        inference_config: dict[str, Any] = {
            "maxTokens": max_tokens,
            "temperature": temperature,
        }
        # Provider "advanced config" sampling overrides -- the Converse API
        # inferenceConfig supports temperature/topP only (topK would be
        # rejected as an unknown field).
        self._merge_sampling_overrides(
            inference_config,
            mapping={"top_p": "topP", "top_k": "_unsupported_top_k"},
        )
        inference_config.pop("_unsupported_top_k", None)

        params: dict[str, Any] = {
            "modelId": self.model,
            "messages": converted_messages,
            "inferenceConfig": inference_config,
        }

        if system_message:
            params["system"] = [{"text": system_message}]

        if tools:
            params["toolConfig"] = {
                "tools": self._convert_tools(tools),
            }
            if tool_choice == "any":
                params["toolConfig"]["toolChoice"] = {"any": {}}
            elif tool_choice == "none":
                params["toolConfig"]["toolChoice"] = {"none": {}}

        # Anthropic Claude models on Bedrock support extended thinking via
        # provider-specific request fields.
        model_lower = self.model.lower()
        if "claude" in model_lower or "anthropic" in model_lower:
            if self.thinking_mode == "disabled":
                params["additionalModelRequestFields"] = {
                    "thinking": {"type": "disabled"},
                }
            elif self.thinking_mode == "adaptive":
                # Adaptive mode does not accept budget_tokens; it optionally
                # accepts an effort level via outputConfig.
                params["additionalModelRequestFields"] = {
                    "thinking": {"type": "adaptive"},
                }
                effort = (self.thinking_effort or "").lower()
                if effort in {"low", "medium", "high"}:
                    params["outputConfig"] = {"effort": effort}
            else:
                budget = int(self.thinking_budget_tokens or 16000)
                budget = max(1024, min(budget, 63999))
                params["additionalModelRequestFields"] = {
                    "thinking": {
                        "type": "enabled",
                        "budget_tokens": budget,
                    }
                }

        try:
            loop = asyncio.get_running_loop()

            if stream:
                # boto3 is synchronous; run the streaming call off-thread.
                response = await loop.run_in_executor(
                    None,
                    lambda: self._client.converse_stream(**params),
                )

                stream = response.get("stream", [])
                finish_reason: str = "stop"
                tool_use_buffer: dict[str, Any] | None = None
                tool_index: int = 0
                thinking_block: bool = False  # current content block is thinking
                _usage: dict[str, int] | None = None
                # ``thinking_block`` tracks whether the in-progress content block
                # is a reasoning block so that deltas are routed to
                # create_backend_thinking vs create_backend_text.

                # boto3's EventStream is synchronous: iterating it inline would
                # block the event loop for the whole response.  Pump events
                # from a worker thread into an asyncio.Queue and consume them
                # asynchronously here.
                queue: asyncio.Queue[Any] = asyncio.Queue()
                _stream_end: object = object()

                def _pump_stream() -> None:
                    try:
                        for event in stream:
                            loop.call_soon_threadsafe(queue.put_nowait, event)
                    except BaseException as exc:  # surfaced to the consumer
                        loop.call_soon_threadsafe(queue.put_nowait, exc)
                    finally:
                        loop.call_soon_threadsafe(queue.put_nowait, _stream_end)

                loop.run_in_executor(None, _pump_stream)

                try:
                    while True:
                        item = await queue.get()
                        if item is _stream_end:
                            break
                        if isinstance(item, BaseException):
                            raise item
                        event = item

                        if "contentBlockStart" in event:
                            # A new content block begins; detect tool vs thinking.
                            start = event["contentBlockStart"]
                            tool_use = start.get("toolUse", {})
                            thinking_data = start.get("thinking", {})
                            if tool_use:
                                thinking_block = False
                                tool_use_buffer = {
                                    "toolUseId": tool_use.get("toolUseId", ""),
                                    "name": tool_use.get("name", ""),
                                    "input": "",
                                }
                                yield create_backend_tool_call_delta(tool_index, "name", tool_use.get("name", ""))
                            elif thinking_data:
                                thinking_block = True
                                # Initial thinking text from contentBlockStart
                                text = thinking_data.get("text", "")
                                if text:
                                    yield create_backend_thinking(text)
                            else:
                                thinking_block = False

                        elif "contentBlockDelta" in event:
                            delta = event["contentBlockDelta"]
                            if thinking_block:
                                th_text = delta.get("delta", {}).get("text", "")
                                if th_text:
                                    yield create_backend_thinking(th_text)
                            else:
                                text_delta = delta.get("delta", {}).get("text", "")
                                if text_delta:
                                    yield create_backend_text(text_delta)
                            tool_input_delta = delta.get("delta", {}).get("toolInput", {}).get("input", "")
                            if tool_input_delta and tool_use_buffer is not None:
                                if isinstance(tool_input_delta, str):
                                    tool_use_buffer["input"] += tool_input_delta
                                td = tool_input_delta
                                arg_val = td if isinstance(td, str) else json.dumps(td)
                                yield create_backend_tool_call_delta(
                                    tool_index,
                                    "arguments",
                                    arg_val,
                                )

                        elif "contentBlockStop" in event:
                            if tool_use_buffer is not None:
                                buf_input = tool_use_buffer["input"]
                                arg_str = buf_input if isinstance(buf_input, str) else json.dumps(buf_input)
                                yield create_backend_tool_call(
                                    id=tool_use_buffer["toolUseId"],
                                    name=tool_use_buffer["name"],
                                    arguments=arg_str,
                                )
                                tool_use_buffer = None
                                tool_index += 1

                        elif "messageStop" in event:
                            # End of the message; normalize the stop reason.
                            stop_reason = event["messageStop"].get("stopReason", "")
                            if stop_reason == "end_turn":
                                finish_reason = "stop"
                            elif stop_reason == "tool_use":
                                finish_reason = "tool_calls"
                            elif stop_reason == "max_tokens":
                                finish_reason = "max_tokens"
                            else:
                                finish_reason = stop_reason or "stop"

                        elif "internalServerException" in event:
                            yield create_backend_error("Bedrock internal server error")
                            return

                        elif "metadata" in event:
                            meta_usage = event["metadata"].get("usage", {})
                            if meta_usage:
                                _usage = {
                                    "input_tokens": meta_usage.get("inputTokens", 0),
                                    "output_tokens": meta_usage.get("outputTokens", 0),
                                }
                finally:
                    # Unblock the pump thread if the consumer stops early.
                    stream_close = getattr(stream, "close", None)
                    if stream_close is not None:
                        try:
                            stream_close()
                        except Exception:
                            logger.debug("exception suppressed", exc_info=True)
                yield create_backend_finish(finish_reason, usage=_usage)

            else:
                # Non-streaming path: one blocking Converse call off-thread.
                response = await loop.run_in_executor(
                    None,
                    lambda: self._client.converse(**params),
                )

                output = response.get("output", {})
                message = output.get("message", {})
                content = message.get("content", [])

                for block in content:
                    if "thinking" in block:
                        yield create_backend_thinking(block["thinking"].get("text", ""))
                    elif "text" in block:
                        yield create_backend_text(block["text"])
                    elif "toolUse" in block:
                        tool_use = block["toolUse"]
                        yield create_backend_tool_call(
                            id=tool_use.get("toolUseId", ""),
                            name=tool_use.get("name", ""),
                            arguments=json.dumps(tool_use.get("input", {})),
                        )

                stop_reason = response.get("stopReason", "end_turn")
                usage_raw = response.get("usage", {})
                _non_stream_usage: dict[str, int] | None = None
                if usage_raw:
                    _non_stream_usage = {
                        "input_tokens": usage_raw.get("inputTokens", 0),
                        "output_tokens": usage_raw.get("outputTokens", 0),
                    }
                if stop_reason == "end_turn":
                    yield create_backend_finish("stop", usage=_non_stream_usage)
                elif stop_reason == "tool_use":
                    yield create_backend_finish("tool_calls", usage=_non_stream_usage)
                else:
                    yield create_backend_finish(stop_reason or "stop", usage=_non_stream_usage)

        except Exception as e:
            yield create_backend_error(str(e))

    def supports_tool_calling(self) -> bool:
        """Bedrock Converse API supports tool calling for Claude and Llama models."""
        return True

    def context_window_size(self) -> int:
        """Return context window for Bedrock models.

        2026: Claude 5.x/4.8/4.7/4.6: 1M, Haiku 4.5: 200K,
        Llama 4 Scout: 1M, Llama 3.3 70B: 128K,
        Nova Premier/Pro/Lite: 300K, Nova Micro: 128K.
        """
        m = self.model.lower()
        # Llama 4 Scout exposes a 1M window on Bedrock.
        if "llama4-scout" in m or "llama-4-scout" in m:
            return 1_048_576
        if "llama3-3" in m or "llama-3-3" in m:
            return 128_000
        if "nova-premier" in m or "nova-pro" in m or "nova-lite" in m:
            return 300_000
        if "nova-micro" in m:
            return 128_000
        # Claude 5.x / 4.8 / 4.7 / Sonnet 4.6 all expose a 1M window.
        if "opus-5" in m or "sonnet-5" in m or "fable-5" in m or "opus-4-8" in m or "opus-4-7" in m or "sonnet-4-6" in m:
            return 1_000_000
        return 200_000

    async def aclose(self) -> None:
        """Close the Bedrock client if initialised."""
        if self._client is not None:
            try:
                import asyncio

                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._client.close)
            except Exception:
                logger.debug("exception suppressed", exc_info=True)
        self._client = None

    def supports_thinking(self) -> bool:
        """Bedrock supports thinking for Claude Opus and Sonnet models."""
        return True

    # ── Multimodal capability declarations ───────────────────────────────
    # Bedrock exposes image generation (Stability / Titan Image Generator)
    # and text embeddings (Titan Embeddings, Cohere Embed) via the
    # ``InvokeModel`` API.  Vision input is supported through the Converse
    # API used by :meth:`chat`.  TTS and STT are *not* Bedrock features
    # (they live in Amazon Polly / Transcribe) and therefore return
    # ``False``.

    def supports_image_generation(self) -> bool:
        return True

    def supports_image_edit(self) -> bool:
        return True

    def supports_embeddings(self) -> bool:
        return True

    def supports_vision_input(self) -> bool:
        return True

    async def _invoke_model(self, model_id: str, body: dict[str, Any], content_type: str = "application/json"):
        """Invoke a Bedrock model synchronously (in an executor)."""
        import asyncio

        await self._ensure_client()
        loop = asyncio.get_running_loop()

        def _call():
            return self._client.invoke_model(
                modelId=model_id,
                contentType=content_type,
                accept="application/json",
                body=json.dumps(body).encode("utf-8"),
            )

        response = await loop.run_in_executor(None, _call)
        body_bytes = response.get("body").read()
        return json.loads(body_bytes)

    # ── Image generation ────────────────────────────────────────────────

    async def generate_image(
        self,
        prompt: str,
        model: str | None = None,
        n: int = 1,
        size: str = "1024x1024",
        quality: str = "standard",
        _response_format: str = "b64_json",
        _style: str | None = None,
        _user: str | None = None,
        extra_params: dict[str, Any] | None = None,
    ):
        """Generate an image via Bedrock (Stability or Titan Image Generator).

        Uses the Stability Diffusion model by default (``stability.stable-image-core-v1:1``)
        and falls back to Titan Image Generator (``amazon.titan-image-generator-v2``)
        for environments where Stability is not available.
        """

        from encre.utils.types import ImageGenerationResponse, ImageResult

        width, height = self._parse_size(size)
        model_id = model or "stability.stable-image-core-v1:1"
        body: dict[str, Any] = {
            "prompt": prompt,
            "seed": 0,
            "output_format": "png",
        }
        if extra_params:
            body.update(extra_params)
        # Stability uses different field names; map if needed.
        if model_id.startswith("stability."):
            body["aspect_ratio"] = self._to_stability_aspect(width, height)
        else:
            body["width"] = width
            body["height"] = height
            body["numberOfImages"] = max(1, min(int(n), 5))
            body["quality"] = quality or "standard"

        payload = await self._invoke_model(model_id, body)
        images: list[ImageResult] = []
        if model_id.startswith("stability."):
            for item in payload.get("images", []) or []:
                if isinstance(item, str):
                    images.append(
                        ImageResult(
                            b64_json=item,
                            mime_type="image/png",
                            model=model_id,
                            provider="bedrock",
                            width=width,
                            height=height,
                        )
                    )
                elif isinstance(item, dict):
                    images.append(
                        ImageResult(
                            b64_json=item.get("b64", ""),
                            mime_type=item.get("mime", "image/png"),
                            model=model_id,
                            provider="bedrock",
                            width=width,
                            height=height,
                        )
                    )
        else:
            for item in payload.get("images", []) or []:
                b64_value = item.get("b64", "") if isinstance(item, dict) else ""
                images.append(
                    ImageResult(
                        b64_json=b64_value,
                        mime_type="image/png",
                        model=model_id,
                        provider="bedrock",
                        width=width,
                        height=height,
                    )
                )
        return ImageGenerationResponse(
            created=0,
            data=images,
            provider="bedrock",
            model=model_id,
        )

    @staticmethod
    def _parse_size(size: str) -> tuple[int, int]:
        """Parse ``"WxH"`` into integer dimensions with sensible defaults."""
        try:
            w, h = size.lower().split("x", 1)
            return int(w), int(h)
        except Exception:
            return 1024, 1024

    @staticmethod
    def _to_stability_aspect(width: int, height: int) -> str:
        """Map a pixel size to the closest Stability aspect ratio token."""
        ratios = {
            (1024, 1024): "1:1",
            (1152, 896): "5:4",
            (1216, 832): "3:2",
            (1344, 768): "16:9",
            (1536, 640): "21:9",
            (896, 1152): "4:5",
            (832, 1216): "2:3",
            (768, 1344): "9:16",
            (640, 1536): "9:21",
        }
        return ratios.get((width, height), "1:1")

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
        """Generate embeddings via Amazon Titan Embeddings 

from encre.logging_config import get_logger


logger = get_logger(__name__)
or Cohere Embed."""
        from encre.utils.types import EmbeddingResponse, EmbeddingResult

        texts = [input] if isinstance(input, str) else list(input)

        model_id = model or "amazon.titan-embed-text-v2:0"
        embeddings: list[EmbeddingResult] = []
        for idx, text in enumerate(texts):
            if model_id.startswith("amazon.titan-embed"):
                body: dict[str, Any] = {"inputText": text}
                if dimensions is not None:
                    body["dimensions"] = dimensions
                if extra_params:
                    body.update(extra_params)
                payload = await self._invoke_model(model_id, body)
                embedding = payload.get("embedding", []) or []
            elif model_id.startswith("cohere.embed"):
                body = {
                    "texts": [text],
                    "input_type": "search_document",
                }
                if dimensions is not None:
                    body["output_dimension"] = dimensions
                if extra_params:
                    body.update(extra_params)
                payload = await self._invoke_model(model_id, body)
                embeddings_raw = payload.get("embeddings", []) or []
                embedding = embeddings_raw[0] if embeddings_raw and isinstance(embeddings_raw[0], list) else []
            else:
                body = {"inputText": text}
                if dimensions is not None:
                    body["dimensions"] = dimensions
                payload = await self._invoke_model(model_id, body)
                embedding = payload.get("embedding", []) or []

            embeddings.append(
                EmbeddingResult(
                    index=idx,
                    embedding=list(embedding),
                    object="embedding",
                    model=model_id,
                )
            )
        return EmbeddingResponse(
            object="list",
            data=embeddings,
            model=model_id,
            provider="bedrock",
            usage={},
        )
