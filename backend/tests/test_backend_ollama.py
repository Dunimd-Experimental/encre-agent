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

"""Tests for OllamaBackend -- construction, capabilities, context window, tokens.

The suite instantiates OllamaBackend without any API key and checks the
localhost base URL default, remote-host and model overrides, and the optional
http_timeout kwarg. It then asserts the inherited OpenAISSEBackend capability
predicates and request-building behaviour (max_tokens, model injection, tool
definitions, stream and temperature defaults), the conservative 8,192-token
context window, token counting, and the idempotent async close lifecycle.
"""

import asyncio

from encre.backends.ollama import OllamaBackend

# ===========================================================================
# Construction
# ===========================================================================


class TestOllamaBackendConstruction:
    """Validate OllamaBackend construction against its locally-tuned defaults.

    Covers the default model and base URL, base_url overrides for remote
    instances, several concrete model names, the optional http_timeout
    kwarg, and a fully custom remote-host configuration. Ollama runs
    locally, so no API key is required at any point.
    """

    def test_create_default(self):
        """Verify the default model is llama4 and the base URL is localhost:11434/v1."""
        be = OllamaBackend()
        assert be.model == "llama4"
        assert be.api_base_url == "http://localhost:11434/v1"
        assert be.api_key == ""

    def test_create_with_base_url(self):
        """Verify a custom base_url pointing at a remote Ollama instance is stored."""
        be = OllamaBackend(base_url="http://192.168.1.100:11434/v1")
        assert be.api_base_url == "http://192.168.1.100:11434/v1"

    def test_create_with_model(self):
        """Verify a llama3.1 model name is stored (must exist in the local Ollama library)."""
        be = OllamaBackend(model="llama3.1")
        assert be.model == "llama3.1"

    def test_create_with_qwen_model(self):
        """Verify the Qwen 2.5 model name is stored."""
        be = OllamaBackend(model="qwen2.5")
        assert be.model == "qwen2.5"

    def test_create_with_mistral_model(self):
        """Verify the Mistral model name is stored."""
        be = OllamaBackend(model="mistral")
        assert be.model == "mistral"

    def test_create_no_api_key_required(self):
        """Verify Ollama runs locally, so no API key is required."""
        be = OllamaBackend()
        assert be.api_key == ""

    def test_create_with_http_timeout(self):
        """Verify the http_timeout kwarg is forwarded to the backend instance."""
        be = OllamaBackend(http_timeout=60.0)
        assert be.http_timeout == 60.0

    def test_create_full_custom(self):
        """Verify a fully custom configuration: remote host, specific model, long timeout."""
        be = OllamaBackend(
            base_url="http://gpu-server:11434/v1",
            model="deepseek-r1:8b",
            http_timeout=300.0,
        )
        assert be.api_base_url == "http://gpu-server:11434/v1"
        assert be.model == "deepseek-r1:8b"
        assert be.http_timeout == 300.0


# ===========================================================================
# Capability checks
# ===========================================================================


class TestOllamaBackendCapabilities:
    """Validate the capability predicates: supports_tool_calling, supports_thinking,
    and supports_prompt_caching.

    Ollama serves whatever model is loaded locally, so the exact flags depend on
    the model roster; these tests assert boolean typing and the inherited
    defaults rather than provider-guaranteed behaviour.
    """

    def test_supports_tool_calling_returns_bool(self):
        """Verify tool calling reports as a boolean, since it depends on the model."""
        be = OllamaBackend()
        result = be.supports_tool_calling()
        assert isinstance(result, bool)

    def test_supports_tool_calling_default_true(self):
        """Verify the inherited OpenAISSEBackend default of True is in effect."""
        be = OllamaBackend()
        assert be.supports_tool_calling() is True

    def test_supports_thinking_returns_bool(self):
        """Verify the thinking support flag is a boolean."""
        be = OllamaBackend()
        result = be.supports_thinking()
        assert isinstance(result, bool)

    def test_supports_prompt_caching_returns_bool(self):
        """Verify prompt caching is reported as a boolean (not supported locally)."""
        be = OllamaBackend()
        result = be.supports_prompt_caching()
        assert isinstance(result, bool)


# ===========================================================================
# Context window size
# ===========================================================================


class TestOllamaBackendContextWindow:
    """Validate context_window_size() across Ollama models.

    The backend reports a single conservative default rather than per-model
    limits, so every model name maps to the same 8,192-token window.
    """

    def test_context_window_size_default(self):
        """Verify the conservative 8192 default is used as a safe value for local models."""
        be = OllamaBackend()
        assert be.context_window_size() == 8192

    def test_context_window_size_different_models(self):
        """Verify the context window is 8192 regardless of model (conservative default)."""
        models = ["llama3.2", "llama3.1", "qwen2.5", "mistral", "deepseek-r1:8b"]
        for m in models:
            be = OllamaBackend(model=m)
            assert be.context_window_size() == 8192, f"model={m}"

    def test_context_window_positive(self):
        """Verify the context window is always a positive integer."""
        be = OllamaBackend()
        assert be.context_window_size() > 0
        assert isinstance(be.context_window_size(), int)


# ===========================================================================
# Token counting and model attribute
# ===========================================================================


class TestOllamaBackendTokens:
    """Validate count_tokens() resilience and model attribute access.

    Token counting must return an int without raising for normal text, empty
    strings, and long repeated text; the model attribute must mirror the
    constructor argument and default to llama4.
    """

    def test_count_tokens_returns_int(self):
        """Verify count_tokens returns an integer for normal text."""
        be = OllamaBackend()
        result = be.count_tokens("hello world")
        assert isinstance(result, int)

    def test_count_tokens_empty_string(self):
        """Verify an empty string does not crash the tokenizer."""
        be = OllamaBackend()
        result = be.count_tokens("")
        assert isinstance(result, int)

    def test_count_tokens_long_text(self):
        """Verify long repeated text does not crash the tokenizer."""
        be = OllamaBackend()
        result = be.count_tokens("Ollama local model token counting. " * 200)
        assert isinstance(result, int)

    def test_model_attribute(self):
        """Verify the model attribute matches the constructor argument."""
        be = OllamaBackend(model="mistral")
        assert be.model == "mistral"
        assert isinstance(be.model, str)

    def test_model_default(self):
        """Verify the default model is llama4."""
        be = OllamaBackend()
        assert be.model == "llama4"


# ===========================================================================
# Request data building
# ===========================================================================


class TestOllamaBackendRequestBuilding:
    """Validate _build_request_data inherited from OpenAISSEBackend.

    Covers max_tokens and model injection, tool definitions with tool_choice,
    the omission of tool fields when no tools are supplied, and the default
    stream and temperature values.
    """

    def test_build_request_includes_max_tokens(self):
        """Verify the request body includes max_tokens."""
        be = OllamaBackend()
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
            max_tokens=512,
        )
        assert data["max_tokens"] == 512

    def test_build_request_includes_model(self):
        """Verify the request body includes the model name."""
        be = OllamaBackend(model="qwen2.5")
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert data["model"] == "qwen2.5"

    def test_build_request_with_tools(self):
        """Verify tool definitions are included when provided."""
        be = OllamaBackend()
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        data = be._build_request_data(
            messages=[{"role": "user", "content": "read the file"}],
            tools=tools,
        )
        assert "tools" in data
        assert data["tools"] == tools
        assert "tool_choice" in data

    def test_build_request_without_tools(self):
        """Verify tools and tool_choice are omitted when no tools are provided."""
        be = OllamaBackend()
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert "tools" not in data
        assert "tool_choice" not in data

    def test_build_request_default_stream(self):
        """Verify streaming is enabled by default."""
        be = OllamaBackend()
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert data["stream"] is True

    def test_build_request_default_temperature(self):
        """Verify the default temperature is 0.0."""
        be = OllamaBackend()
        data = be._build_request_data(
            messages=[{"role": "user", "content": "hello"}],
        )
        assert data["temperature"] == 0.0


# ===========================================================================
# Lifecycle
# ===========================================================================


class TestOllamaBackendLifecycle:
    """Validate resource cleanup and close idempotency."""

    def test_aclose_does_not_raise(self):
        """Verify aclose() works without a prior request (lazy client)."""
        be = OllamaBackend()
        asyncio.run(be.aclose())

    def test_aclose_idempotent(self):
        """Verify aclose() called twice does not raise."""

        async def _double():
            """Close the backend twice in sequence."""
            be = OllamaBackend()
            await be.aclose()
            await be.aclose()

        asyncio.run(_double())
