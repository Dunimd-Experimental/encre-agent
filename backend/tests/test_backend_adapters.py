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

"""Tests for the model-adapter drop-in registry (P1.2).

The factory under test is the **real** ``create_backend`` -- the same
public entry point the loop, autosafety and the config layer call -- now
delegating to :class:`~encre.backends.adapter_registry.AdapterRegistry`
instead of the former hard-coded ``backend_map``.

Covered invariants:

1. every ``backend_type`` the former chain supported still resolves to the
   SAME backend class (parametrised over the full historical list -- the
   no-behaviour-change proof);
2. an unknown ``backend_type`` raises the same ``ValueError`` with the
   same message shape as before;
3. same-name re-registration replaces the adapter in place and
   ``resolve()`` returns the replacement;
4. a custom adapter registered through the public
   ``register_backend_adapter`` API is selected with zero core-file
   changes (the fixture is a real ``BaseBackend`` subclass driving the
   genuine async-generator streaming path -- no internals mocked);
5. dispatch precedence is registration order: the first registered match
   wins;
6. a custom adapter's ``model_info`` enriches ``resolve_model_info`` for
   models the static registry does not know, and unregistering restores
   the previous resolution.
"""

import pytest

from encre.backend import create_backend
from encre.backends import (  # public drop-in import path (acceptance criterion)
    BackendAdapter,
    BackendConfig,
    register_backend_adapter,
    unregister_backend_adapter,
)
from encre.backends.adapter_api import BackendConfig as _BackendConfigModule
from encre.backends.adapter_registry import AdapterRegistry, default_registry
from encre.backends.aigateway import AIGatewayBackend
from encre.backends.alibaba import AlibabaBackend
from encre.backends.anthropic import AnthropicBackend
from encre.backends.arcee import ArceeBackend
from encre.backends.base import BaseBackend
from encre.backends.bedrock import BedrockBackend
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
from encre.backends.opencode import OpenCodeGoBackend, OpenCodeZenBackend
from encre.backends.openrouter import OpenRouterBackend
from encre.backends.registry import ModelInfo, resolve_model_info
from encre.backends.router import RouterBackend
from encre.backends.tencent import TencentBackend
from encre.backends.volcengine import VolcengineArkBackend
from encre.backends.xiaomi import XiaomiBackend
from encre.utils.types import BackendFinish, BackendText

# The exact names (sorted) the default adapter registry carries --
# the unknown-type error message lists them, so this set is contract.
# (Gateway providers verified against official docs live in
# encre.backends.gateway_providers.)
_HISTORICAL_TYPES = sorted([
    "openai", "anthropic", "ollama", "deepseek", "google", "groq",
    "local", "bedrock", "openai_compatible", "failover", "router",
    "openrouter", "novita", "aigateway", "glm", "kimi", "arcee", "gmi",
    "minimax", "alibaba", "kilocode", "xiaomi", "tencent", "huggingface",
    "opencode-zen", "opencode-go", "lmstudio", "github-copilot",
    "volcengine-ark",
    "302ai", "atomic-chat", "azure-openai", "azure-cognitive", "baseten",
    "cerebras", "cortecs", "deep-infra", "fireworks", "frogbot",
    "helicone", "ionet", "llamacpp", "nebius", "ollama-cloud", "ovhcloud",
    "scaleway", "stackit", "together", "venice", "vercel", "xai", "zai",
    "zenmux",
])

_API_KEY = "sk-adapter-test"
_BASE_URL = "https://unit-test.invalid/v1"


def test_public_dropin_import_path_exposes_the_contract():
    """`from encre.backends import register_backend_adapter` works (acceptance)."""
    assert register_backend_adapter is not None
    assert unregister_backend_adapter is not None
    assert BackendConfig is _BackendConfigModule
    assert hasattr(BackendAdapter, "matches")
    assert hasattr(BackendAdapter, "create")
    assert hasattr(BackendAdapter, "model_info")
    # Structural protocol: the built-in SimpleBackendAdapter satisfies it.
    openai_adapter = default_registry.get("openai")
    assert isinstance(openai_adapter.name, str)
    assert openai_adapter.matches(BackendConfig(backend_type="openai"))
    assert not openai_adapter.matches(BackendConfig(backend_type="anthropic"))


def _simple_kwargs(backend_type: str) -> dict:
    """Minimal constructor kwargs per backend type (constructor-compatible)."""
    if backend_type == "local":
        # LocalBackend forwards extra kwargs to transformers.pipeline.
        return {}
    if backend_type == "openai_compatible":
        return {"base_url": _BASE_URL, "api_key": _API_KEY}
    if backend_type == "router":
        child = OpenAIBackend(api_key=_API_KEY)
        return {"routes": {"code": child}, "default": OpenAIBackend(api_key=_API_KEY)}
    if backend_type == "failover":
        return {"backends": [("primary", OpenAIBackend(api_key=_API_KEY))]}
    return {"api_key": _API_KEY}


_EXPECTED_CLASSES = {
    "openai": OpenAIBackend,
    "anthropic": AnthropicBackend,
    "ollama": OllamaBackend,
    "deepseek": DeepSeekBackend,
    "google": GoogleBackend,
    "groq": GroqBackend,
    "local": LocalBackend,
    "bedrock": BedrockBackend,
    "openai_compatible": OpenAICompatibleBackend,
    "failover": FailoverBackend,
    "router": RouterBackend,
    "openrouter": OpenRouterBackend,
    "novita": NovitaBackend,
    "aigateway": AIGatewayBackend,
    "glm": GLMBackend,
    "kimi": KimiBackend,
    "arcee": ArceeBackend,
    "gmi": GMIBackend,
    "minimax": MiniMaxBackend,
    "alibaba": AlibabaBackend,
    "kilocode": KiloCodeBackend,
    "xiaomi": XiaomiBackend,
    "tencent": TencentBackend,
    "huggingface": HuggingFaceBackend,
    "opencode-zen": OpenCodeZenBackend,
    "opencode-go": OpenCodeGoBackend,
    "lmstudio": LMStudioBackend,
    "github-copilot": GitHubCopilotBackend,
    "volcengine-ark": VolcengineArkBackend,
    "302ai": OpenAI302Backend,
    "atomic-chat": AtomicChatBackend,
    "azure-openai": AzureOpenAIBackend,
    "azure-cognitive": AzureCognitiveBackend,
    "baseten": BasetenBackend,
    "cerebras": CerebrasBackend,
    "cortecs": CortecsBackend,
    "deep-infra": DeepInfraBackend,
    "fireworks": FireworksBackend,
    "frogbot": FrogBotBackend,
    "helicone": HeliconeBackend,
    "ionet": IONetBackend,
    "llamacpp": LlamaCppBackend,
    "nebius": NebiusBackend,
    "ollama-cloud": OllamaCloudBackend,
    "ovhcloud": OVHCloudBackend,
    "scaleway": ScalewayBackend,
    "stackit": STACKITBackend,
    "together": TogetherBackend,
    "venice": VeniceBackend,
    "vercel": VercelAIGatewayBackend,
    "xai": XAIBackend,
    "zai": ZAIBackend,
    "zenmux": ZenMuxBackend,
}


# ----------------------------------------------------------------------
# 1 -- every historical backend_type resolves to the SAME backend class
# ----------------------------------------------------------------------

@pytest.mark.parametrize("backend_type", _HISTORICAL_TYPES)
def test_every_historical_type_resolves_to_same_class(backend_type):
    """The adapter registry reproduces the former dispatch one-for-one."""
    be = create_backend(backend_type, **_simple_kwargs(backend_type))
    assert isinstance(be, BaseBackend)
    assert isinstance(be, _EXPECTED_CLASSES[backend_type]), (
        f"backend_type {backend_type!r} must resolve to "
        f"{_EXPECTED_CLASSES[backend_type].__name__}, got {type(be).__name__}"
    )


def test_builtin_registry_covers_exact_historical_set():
    """The default registry carries exactly the former dispatch names."""
    assert sorted(default_registry.names()) == _HISTORICAL_TYPES


# ----------------------------------------------------------------------
# 2 -- unknown backend_type: same error contract as before
# ----------------------------------------------------------------------

def test_unknown_backend_type_raises_same_valueerror():
    """Unknown types raise ValueError with the historical message shape."""
    with pytest.raises(ValueError) as excinfo:
        create_backend("nonexistent_backend")
    message = str(excinfo.value)
    assert message == (
        f"Unknown backend type: nonexistent_backend. Available: {_HISTORICAL_TYPES}"
    )


# ----------------------------------------------------------------------
# 3 -- same-name re-registration replaces in place
# ----------------------------------------------------------------------

class _ReplaceFirstAdapter:
    """First of two adapters sharing a name; creates a backend tagged "first"."""

    name = "swap-type"

    def matches(self, config: BackendConfig) -> bool:
        """Claim requests whose backend_type equals this adapter's name."""
        return config.backend_type == self.name

    def create(self, config: BackendConfig) -> BaseBackend:
        """Build an OpenAIBackend whose api_key and model are tagged "first"."""
        return OpenAIBackend(api_key="first", model="first-model")

    def model_info(self, model_name, config=None):
        """Decline to describe any model, letting the static registry answer."""
        return None


class _ReplaceSecondAdapter(_ReplaceFirstAdapter):
    """Same-named replacement whose created backend is tagged "second"."""

    def create(self, config: BackendConfig) -> BaseBackend:
        """Build an OpenAIBackend whose api_key and model are tagged "second"."""
        return OpenAIBackend(api_key="second", model="second-model")


def test_same_name_reregistration_replaces():
    """Re-registering a name swaps the adapter; resolve() hits the new one."""
    registry = AdapterRegistry()
    registry.register(_ReplaceFirstAdapter())
    first = registry.resolve(BackendConfig(backend_type="swap-type"))
    assert first.api_key == "first"

    registry.register(_ReplaceSecondAdapter())
    assert registry.get("swap-type").name == "swap-type"
    assert len(registry.all()) == 1, "same-name registration must replace, not append"

    second = registry.resolve(BackendConfig(backend_type="swap-type"))
    assert second.api_key == "second", "resolve() must return the replacement"
    assert second.model == "second-model"


# ----------------------------------------------------------------------
# 4 -- public drop-in API: custom backend, zero core-file changes
# ----------------------------------------------------------------------

class _EchoBackend(BaseBackend):
    """Real minimal ``BaseBackend`` streaming one canned text response.

    Subclassing the genuine ABC (not mocking) keeps the created backend on
    the authentic streaming path -- the acceptance proof that a third
    party can ship a working backend with one adapter class.
    """

    def __init__(self, model: str = "echo") -> None:
        """Store the model name and reset the chat-call counter."""
        self.model = model
        self.chat_calls = 0

    async def chat(self, messages, tools=None, tool_choice="auto",
                   temperature=0.0, max_tokens=4096, stream=True,
                   enable_caching=False, cache_edits_state=None):
        """Yield one echo text event followed by a stop finish event."""
        self.chat_calls += 1
        yield BackendText(text=f"echo:{self.model}")
        yield BackendFinish(reason="stop", usage=None)

    def supports_tool_calling(self) -> bool:
        """Report that the echo backend cannot call tools."""
        return False

    def context_window_size(self) -> int:
        """Report the small fixed window this test backend claims."""
        return 4096

    def supports_prompt_caching(self) -> bool:
        """Report that the echo backend offers no prompt caching."""
        return False


class _EchoAdapter:
    """Adapter routing ``test-echo`` requests to the canned echo backend."""

    name = "test-echo"

    def matches(self, config: BackendConfig) -> bool:
        """Claim requests whose backend_type is the echo adapter's name."""
        return config.backend_type == self.name

    def create(self, config: BackendConfig) -> BaseBackend:
        """Create an echo backend carrying the requested model name."""
        return _EchoBackend(model=config.model)

    def model_info(self, model_name, config=None):
        """Decline to describe models; the static registry must handle them."""
        return None


def test_custom_adapter_dropin_via_public_api():
    """register_backend_adapter makes a new backend_type work end to end."""
    try:
        register_backend_adapter(_EchoAdapter())
        assert "test-echo" in default_registry.names()

        be = create_backend("test-echo", model="third-party-model")
        assert isinstance(be, _EchoBackend), "the custom adapter must be selected"
        assert be.model == "third-party-model", "factory kwargs must reach the backend"

        async def _drain():
            """Consume the streaming chat response and collect every event."""
            chunks = []
            async for event in be.chat(messages=[{"role": "user", "content": "hi"}]):
                chunks.append(event)
            return chunks

        import asyncio
        events = asyncio.run(_drain())
        assert isinstance(events[0], BackendText)
        assert events[0].text == "echo:third-party-model"
        assert isinstance(events[1], BackendFinish)
        assert be.chat_calls == 1
    finally:
        assert unregister_backend_adapter("test-echo") is True

    assert unregister_backend_adapter("test-echo") is False, "double remove is a no-op"
    with pytest.raises(ValueError, match="Unknown backend type"):
        create_backend("test-echo")


# ----------------------------------------------------------------------
# 5 -- precedence: first registered match wins
# ----------------------------------------------------------------------

class _MatchAllAdapter:
    """Matches every request -- isolates pure registration-order semantics."""

    def __init__(self, tag: str) -> None:
        """Bind the adapter name and tag from the given label."""
        self.name = f"precedence-{tag}"
        self.tag = tag

    def matches(self, config: BackendConfig) -> bool:
        """Accept every config so only registration order can decide."""
        return True

    def create(self, config: BackendConfig) -> BaseBackend:
        """Create an OpenAIBackend tagged with this adapter's label."""
        return OpenAIBackend(api_key=self.tag, model=self.tag)

    def model_info(self, model_name, config=None):
        """Decline to describe models."""
        return None


def test_first_registered_match_wins():
    """resolve() dispatches to the earliest registered matching adapter."""
    registry = AdapterRegistry()
    first = _MatchAllAdapter("one")
    second = _MatchAllAdapter("two")
    registry.register(first)
    registry.register(second)

    be = registry.resolve(BackendConfig(backend_type="anything", kwargs={}))
    assert be.api_key == "one", "the first registered match must win"
    assert [a.tag for a in registry.all()] == ["one", "two"], (
        "all() must preserve registration order"
    )


# ----------------------------------------------------------------------
# 6 -- model_info enrichment of resolve_model_info
# ----------------------------------------------------------------------

class _DescribeAdapter:
    """Adapter that supplies metadata for unknown ``custom-*`` models."""

    name = "test-describe"

    def matches(self, config: BackendConfig) -> bool:
        """Claim requests whose backend_type is the describe adapter's name."""
        return config.backend_type == self.name

    def create(self, config: BackendConfig) -> BaseBackend:
        """Forward the config kwargs straight into an OpenAIBackend."""
        return OpenAIBackend(**config.kwargs)

    def model_info(self, model_name, config=None):
        """Describe ``custom-*`` models; decline everything else."""
        if model_name.startswith("custom-"):
            return ModelInfo(
                name=model_name, provider="custom-vendor",
                context_window=123456, max_output_tokens=4096,
            )
        return None


def test_adapter_model_info_enriches_resolution():
    """A custom adapter can describe models the static registry misses."""
    try:
        register_backend_adapter(_DescribeAdapter())
        info = resolve_model_info("custom-alpha")
        assert info.provider == "custom-vendor", "adapter enrichment must win"
        assert info.context_window == 123456
        assert info.name == "custom-alpha"

        # Static registry entries keep priority over adapters.
        assert resolve_model_info("gpt-4.1").provider == "openai"
        # Non-matching model names fall through to provider defaults.
        fallback = resolve_model_info("unrelated-model")
        assert fallback.provider == "openai", (
            "adapters declining must restore the default resolution"
        )
    finally:
        unregister_backend_adapter("test-describe")

    after = resolve_model_info("custom-alpha")
    assert after.provider == "openai", "unregistering must undo the enrichment"
