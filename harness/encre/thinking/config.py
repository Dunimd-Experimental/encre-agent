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

"""Thinking configuration resolution.

Decides whether a model should use native "thinking"/reasoning tokens based on
the model name and backend type, and computes the reasoning budget.  Explicit
user configuration always wins; otherwise support is inferred from the
``_THINKING_MODEL_PATTERNS`` and ``_THINKING_BACKEND_TYPES`` tables.
"""

from encre.utils.types import (
    AdaptiveThinking,
    DisabledThinking,
    EnabledThinking,
    ThinkingConfig,
)

# Model name prefixes or keywords that signal native thinking/reasoning support
_THINKING_MODEL_PATTERNS: tuple[str, ...] = (
    "claude", "sonnet", "opus",
    "deepseek", "o3", "o4", "o1",
    "gemini-2.5", "gemini-2.0", "gemini-3",
    "qwen3", "qwen-max",
    "llama-4",
    "yi",
    "gpt-5",
    "hunyuan",
    "doubao",
    "ernie-4",
    "kimi",
    "glm-4",
)

# Backend types known to support thinking tokens
_THINKING_BACKEND_TYPES: tuple[str, ...] = (
    "anthropic", "deepseek", "openrouter", "openai_compatible",
    "google", "glm", "kimi", "minimax", "alibaba", "tencent",
    "xiaomi", "gmi", "bedrock", "volcengine-ark",
    "groq",  # Llama 4 Scout supports reasoning
)


def resolve_thinking_config(
    config: ThinkingConfig | None,
    model: str,
    backend_type: str = "",
) -> ThinkingConfig:
    """Resolve the thinking configuration.

    If an explicit config is provided it is used as-is.  Otherwise, the
    function checks whether the model name or backend type indicates
    thinking support, and enables adaptive thinking if so.

    Args:
        config: Explicit user-provided thinking config, or None.
        model: Model identifier (e.g. "claude-sonnet-4-6-20250514").
        backend_type: Backend type key (e.g. "anthropic", "openai_compatible").

    Returns:
        A :class:`ThinkingConfig` -- either :class:`AdaptiveThinking`
        (enabled), :class:`EnabledThinking`, or :class:`DisabledThinking`.
    """
    if config is not None:
        return config

    model_lower = model.lower()

    # Check by backend type first (most reliable)
    if backend_type in _THINKING_BACKEND_TYPES:
        return AdaptiveThinking(enabled=True, min_tokens=1024, max_tokens=8192)

    # Check by model name patterns
    for pattern in _THINKING_MODEL_PATTERNS:
        if pattern in model_lower:
            return AdaptiveThinking(enabled=True, min_tokens=1024, max_tokens=8192)

    return DisabledThinking(enabled=False)


def get_thinking_budget_tokens(config: ThinkingConfig) -> int:
    """Return the reasoning token budget for *config*.

    Returns the configured max tokens for adaptive thinking, the explicit
    budget for enabled thinking, or ``0`` when thinking is disabled.
    """
    if isinstance(config, AdaptiveThinking):
        return config.max_tokens
    if isinstance(config, EnabledThinking):
        return config.budget_tokens
    return 0
