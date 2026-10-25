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
Backends package -- multi-provider LLM inference adapters.

This package provides a unified interface for communicating with 9 different
LLM providers, each implemented as a subclass of :class:`BaseBackend`. Every
backend normalises the provider's native streaming protocol into a common set
of :class:`BackendEvent` types so that the agent loop can consume text deltas,
tool call deltas, thinking tokens, finish signals, and error events without
caring which provider is underneath.

Provider backends (2026 lineup)
-------------------------------
+--------------------------+-----------------------------------------------+
| Backend                  | Supported models (2026)                        |
+--------------------------+-----------------------------------------------+
| :class:`OpenAIBackend`   | GPT-4.1 family, GPT-5.x, o3, o4-mini          |
| :class:`AnthropicBackend`| Claude Opus 4.6/4.7, Sonnet 4.5/4.6, Haiku 4.5|
| :class:`GoogleBackend`   | Gemini 2.5 Pro, Gemini 2.5 Flash              |
| :class:`DeepSeekBackend` | DeepSeek V4-Flash, V4-Pro                     |
| :class:`GroqBackend`     | Llama 3.3 70B, Llama 4 Scout, GPT-OSS         |
| :class:`OllamaBackend`   | Locally-hosted models via Ollama               |
| :class:`LocalBackend`    | Hugging Face transformers (CPU/GPU)            |
| :class:`BedrockBackend`  | AWS Bedrock Converse API (Claude, Llama, etc.) |
| :class:`OpenAICompatibleBackend` | Any OpenAI-compatible endpoint (vLLM, etc.) |
+--------------------------+-----------------------------------------------+

Shared infrastructure
---------------------
- :class:`OpenAISSEBackend` -- base class for OpenAI-protocol backends (SSE parsing,
  tool call buffering, non-stream fallback). Used by OpenAI, DeepSeek, Groq,
  and OpenAICompatible backends.
- :func:`retry_with_backoff` -- exponential-backoff retry for transient HTTP
  errors (429, 502, 503, 504, timeouts, connection errors).
- :class:`BackendRegistry` -- dynamic model metadata registry with provider-level
  defaults and thread-safe registration/resolution.

Typical usage::

    from encre.backends import OpenAIBackend  # noqa: E402

    backend = OpenAIBackend(model="gpt-5.6")
    async for event in backend.chat(messages=[{"role": "user", "content": "Hello"}]):
        print(event)
"""

# Re-export every backend class and shared helper from this package so callers
# can import them directly from ``encre.backends``.
from encre.backends.aigateway import AIGatewayBackend
from encre.backends.alibaba import AlibabaBackend
from encre.backends.anthropic import AnthropicBackend
from encre.backends.arcee import ArceeBackend
from encre.backends.auth import AuthManager
from encre.backends.base import BaseBackend
from encre.backends.bedrock import BedrockBackend
from encre.backends.catalog import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    PROVIDERS,
    catalog_payload,
    default_output_tokens,
)
from encre.backends.connection import (
    ConnectionErrorCategory,
    ConnectionErrorInfo,
    ConnectionHealthMonitor,
    HeartbeatSession,
    format_connection_error,
)
from encre.backends.deepseek import DeepSeekBackend
from encre.backends.failover import FailoverBackend
from encre.backends.gateway_providers import (
    AtomicChatBackend,
    AzureCognitiveBackend,
    AzureOpenAIBackend,
    BasetenBackend,
    CerebrasBackend,
    CortecsBackend,
    DeepInfraBackend,
    FireworksBackend,
    FrogBotBackend,
    HeliconeBackend,
    IONetBackend,
    LlamaCppBackend,
    NebiusBackend,
    OVHCloudBackend,
    OllamaCloudBackend,
    OpenAI302Backend,
    ScalewayBackend,
    STACKITBackend,
    TogetherBackend,
    VeniceBackend,
    VercelAIGatewayBackend,
    XAIBackend,
    ZAIBackend,
    ZenMuxBackend,
)
from encre.backends.github_copilot import GitHubCopilotBackend
from encre.backends.glm import GLMBackend
from encre.backends.gmi import GMIBackend
from encre.backends.google import GoogleBackend
from encre.backends.groq import GroqBackend
from encre.backends.huggingface import HuggingFaceBackend
from encre.backends.kilocode import KiloCodeBackend
from encre.backends.kimi import KimiBackend
from encre.backends.lmstudio import LMStudioBackend
from encre.backends.local import LocalBackend
from encre.backends.minimax import MiniMaxBackend
from encre.backends.novita import NovitaBackend
from encre.backends.ollama import OllamaBackend
from encre.backends.openai import OpenAIBackend
from encre.backends.openai_compatible import OpenAICompatibleBackend
from encre.backends.openai_sse import OpenAISSEBackend
from encre.backends.opencode import OpenCodeGoBackend, OpenCodeZenBackend
from encre.backends.openrouter import OpenRouterBackend
from encre.backends.retry import (
    DEFAULT_RETRY_CONFIG,
    ErrorClass,
    RetryConfig,
    RetryEvent,
    classify_error,
    retry_with_backoff,
)
from encre.backends.router import CostTracker, RouterBackend, TaskCategory
from encre.backends.tencent import TencentBackend
from encre.backends.volcengine import VolcengineArkBackend
from encre.backends.xiaomi import XiaomiBackend

# P1.2 model-adapter drop-in surface: third-party packages register their
# backend with one call and zero core changes.
from encre.backends.adapter_api import BackendAdapter, BackendConfig
from encre.backends.adapter_registry import (
    AdapterRegistry,
    default_registry,
    register_backend_adapter,
    unregister_backend_adapter,
)

__all__ = [
    "AdapterRegistry",
    "BackendAdapter",
    "BackendConfig",
    "default_registry",
    "register_backend_adapter",
    "unregister_backend_adapter",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_RETRY_CONFIG",
    "ErrorClass",
    "PROVIDERS",
    "AIGatewayBackend",
    "AlibabaBackend",
    "AnthropicBackend",
    "ArceeBackend",
    "AtomicChatBackend",
    "AuthManager",
    "AzureCognitiveBackend",
    "AzureOpenAIBackend",
    "BasetenBackend",
    "BackendHealth",
    "BaseBackend",
    "BedrockBackend",
    "CerebrasBackend",
    "CortecsBackend",
    "CostTracker",
    "DeepInfraBackend",
    "DeepSeekBackend",
    "FailoverBackend",
    "FireworksBackend",
    "FrogBotBackend",
    "GLMBackend",
    "GMIBackend",
    "GitHubCopilotBackend",
    "GoogleBackend",
    "GroqBackend",
    "HeartbeatSession",
    "HeliconeBackend",
    "HuggingFaceBackend",
    "IONetBackend",
    "KiloCodeBackend",
    "KimiBackend",
    "LlamaCppBackend",
    "LMStudioBackend",
    "LocalBackend",
    "MiniMaxBackend",
    "NebiusBackend",
    "NovitaBackend",
    "OllamaBackend",
    "OllamaCloudBackend",
    "OpenAI302Backend",
    "OpenAIBackend",
    "OpenAICompatibleBackend",
    "OpenAISSEBackend",
    "OpenCodeGoBackend",
    "OpenCodeZenBackend",
    "OpenRouterBackend",
    "OVHCloudBackend",
    "RetryConfig",
    "RetryEvent",
    "RouterBackend",
    "ScalewayBackend",
    "STACKITBackend",
    "TaskCategory",
    "TencentBackend",
    "TogetherBackend",
    "VeniceBackend",
    "VercelAIGatewayBackend",
    "XAIBackend",
    "XiaomiBackend",
    "ZAIBackend",
    "ZenMuxBackend",
    "catalog_payload",
    "classify_error",
    "ConnectionErrorCategory",
    "ConnectionErrorInfo",
    "ConnectionHealthMonitor",
    "default_output_tokens",
    "format_connection_error",
    "retry_with_backoff",
]
