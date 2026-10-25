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

"""Lightweight OpenAI-compatible gateway providers.

Every class here is a thin adapter over :class:`OpenAISSEBackend` (or
:class:`GLMBackend` for Z.AI) that only supplies the provider's official
OpenAI-compatible endpoint.  All base URLs were verified against each
provider's OWN documentation (never aggregator sites):

====================  =========================================================  =============================================================
Provider              Base URL                                                   Official docs
====================  =========================================================  =============================================================
302.AI                https://api.302.ai/v1                                      https://doc.302.ai/
Atomic Chat (local)   http://127.0.0.1:1337/v1                                   (desktop app, OpenAI-compatible server)
Azure OpenAI          https://<resource>.openai.azure.com/openai/v1              https://learn.microsoft.com/azure/foundry/openai/
Azure Cognitive       https://<resource>.cognitiveservices.azure.com/openai/v1   https://learn.microsoft.com/azure/ai-services/openai/
Baseten               https://inference.baseten.co/v1                            https://docs.baseten.co/quickstart
Cerebras              https://api.cerebras.ai/v1                                 https://inference-docs.cerebras.ai/
Cortecs               https://api.cortecs.ai/v1                                  https://docs.cortecs.ai/
Deep Infra            https://api.deepinfra.com/v1/openai                        https://docs.deepinfra.com/
Fireworks AI          https://api.fireworks.ai/inference/v1                      https://fireworks.ai/docs/tools-sdks/openai-compatibility
FrogBot (gateway)     http://localhost:3939/v1                                   https://docs.frogbot.ai/
Helicone AI Gateway   https://ai-gateway.helicone.ai                             https://docs.helicone.ai/getting-started/quick-start
IO.NET Intelligence   https://api.intelligence.io.solutions/api/v1               https://io.net/docs/reference/ai-models/get-started-with-io-intelligence-api
llama.cpp (local)     http://127.0.0.1:8080/v1                                   https://github.com/ggml-org/llama.cpp
Nebius Token Factory  https://api.tokenfactory.nebius.com/v1                     https://docs.tokenfactory.nebius.com/quickstart
Ollama Cloud          https://ollama.com/v1                                      https://docs.ollama.com/api/openai-compatibility
OVHcloud AI Endpoints https://oai.endpoints.kepler.ai.cloud.ovh.net/v1           https://docs.ovhcloud.com/
Scaleway Generative   https://api.scaleway.ai/v1                                 https://www.scaleway.com/en/docs/generative-apis/
STACKIT Model Serving https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1  https://docs.stackit.cloud/
Together AI           https://api.together.ai/v1                                 https://docs.together.ai/docs/inference/openai-compatibility
Venice AI             https://api.venice.ai/api/v1                               https://docs.venice.ai/api-reference/endpoint/chat/completions
Vercel AI Gateway     https://ai-gateway.vercel.sh/v1                            https://vercel.com/docs/ai-gateway/openai-compat
xAI (Grok)            https://api.x.ai/v1                                        https://docs.x.ai/docs/api-reference
Z.AI (GLM intl)       https://api.z.ai/api/paas/v4                               https://docs.z.ai/
ZenMux                https://zenmux.ai/api/v1                                   https://zenmux.ai/docs/zh/guide/quickstart.html
====================  =========================================================  =============================================================

Providers whose auth model Encre cannot drive (Google Vertex AI service
accounts, SAP AI Core service keys, GitLab Duo OAuth) are deliberately NOT
listed here: an entry that cannot actually authenticate would be a fake
option, and fake options are worse than missing ones.
"""

from typing import Any

from encre.backends.glm import GLMBackend
from encre.backends.openai_sse import OpenAISSEBackend


class _OpenAICompatBackend(OpenAISSEBackend):
    """Shared base for thin OpenAI-compatible gateway providers.

    Subclasses only declare ``DEFAULT_BASE_URL``; the constructor falls back
    to it whenever the caller supplies no explicit endpoint.  Azure-style
    providers that need a per-account URL declare an empty default instead,
    so a missing URL surfaces as a real connection error instead of a
    silently wrong endpoint.
    """

    DEFAULT_BASE_URL = ""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        **kwargs: Any,
    ) -> None:
        if not base_url:
            base_url = self.DEFAULT_BASE_URL
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)


class OpenAI302Backend(_OpenAICompatBackend):
    """302.AI aggregator -- OpenAI-compatible gateway over 200+ models."""

    DEFAULT_BASE_URL = "https://api.302.ai/v1"


class AtomicChatBackend(_OpenAICompatBackend):
    """Atomic Chat desktop app -- local OpenAI-compatible server."""

    DEFAULT_BASE_URL = "http://127.0.0.1:1337/v1"


class AzureOpenAIBackend(_OpenAICompatBackend):
    """Azure OpenAI -- v1 route under the user's resource endpoint.

    ``base_url`` must be the full resource URL
    (``https://<resource-name>.openai.azure.com/openai/v1``); the ``model``
    field carries the Azure deployment name.
    """

    DEFAULT_BASE_URL = ""


class AzureCognitiveBackend(_OpenAICompatBackend):
    """Azure Cognitive Services / AI Services -- OpenAI v1 route.

    ``base_url`` must be the full resource URL
    (``https://<resource-name>.cognitiveservices.azure.com/openai/v1``);
    the ``model`` field carries the Azure deployment name.
    """

    DEFAULT_BASE_URL = ""


class BasetenBackend(_OpenAICompatBackend):
    """Baseten -- managed and dedicated model inference."""

    DEFAULT_BASE_URL = "https://inference.baseten.co/v1"


class CerebrasBackend(_OpenAICompatBackend):
    """Cerebras Inference -- wafer-scale low-latency serving."""

    DEFAULT_BASE_URL = "https://api.cerebras.ai/v1"


class CortecsBackend(_OpenAICompatBackend):
    """Cortecs -- European serverless LLM inference."""

    DEFAULT_BASE_URL = "https://api.cortecs.ai/v1"


class DeepInfraBackend(_OpenAICompatBackend):
    """Deep Infra -- note the ``/v1/openai`` suffix (not plain ``/v1``)."""

    DEFAULT_BASE_URL = "https://api.deepinfra.com/v1/openai"


class FireworksBackend(_OpenAICompatBackend):
    """Fireworks AI -- model IDs use ``accounts/fireworks/models/...``."""

    DEFAULT_BASE_URL = "https://api.fireworks.ai/inference/v1"


class FrogBotBackend(_OpenAICompatBackend):
    """FrogBot -- self-hosted open-source AI gateway (Vercel AI SDK based)."""

    DEFAULT_BASE_URL = "http://localhost:3939/v1"


class HeliconeBackend(_OpenAICompatBackend):
    """Helicone AI Gateway -- observability gateway with automatic routing."""

    DEFAULT_BASE_URL = "https://ai-gateway.helicone.ai"


class IONetBackend(_OpenAICompatBackend):
    """IO.NET IO Intelligence -- regular inference uses the io.solutions domain."""

    DEFAULT_BASE_URL = "https://api.intelligence.io.solutions/api/v1"


class LlamaCppBackend(_OpenAICompatBackend):
    """llama.cpp ``llama-server`` -- local OpenAI-compatible server."""

    DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"


class NebiusBackend(_OpenAICompatBackend):
    """Nebius Token Factory -- hosted open-weight model inference."""

    DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"


class OllamaCloudBackend(_OpenAICompatBackend):
    """Ollama Cloud -- hosted variant of Ollama (ollama.com, not localhost)."""

    DEFAULT_BASE_URL = "https://ollama.com/v1"


class OVHCloudBackend(_OpenAICompatBackend):
    """OVHcloud AI Endpoints -- managed inference on OVHcloud."""

    DEFAULT_BASE_URL = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"


class ScalewayBackend(_OpenAICompatBackend):
    """Scaleway Generative APIs -- European sovereign inference."""

    DEFAULT_BASE_URL = "https://api.scaleway.ai/v1"


class STACKITBackend(_OpenAICompatBackend):
    """STACKIT AI Model Serving -- German sovereign model serving."""

    DEFAULT_BASE_URL = "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1"


class TogetherBackend(_OpenAICompatBackend):
    """Together AI -- open-model cloud; IDs use ``<provider>/<model>``."""

    DEFAULT_BASE_URL = "https://api.together.ai/v1"


class VeniceBackend(_OpenAICompatBackend):
    """Venice AI -- private, uncensored inference."""

    DEFAULT_BASE_URL = "https://api.venice.ai/api/v1"


class VercelAIGatewayBackend(_OpenAICompatBackend):
    """Vercel AI Gateway -- unified endpoint across providers; IDs use ``provider/model``."""

    DEFAULT_BASE_URL = "https://ai-gateway.vercel.sh/v1"


class XAIBackend(_OpenAICompatBackend):
    """xAI (Grok) -- OpenAI-compatible API at api.x.ai."""

    DEFAULT_BASE_URL = "https://api.x.ai/v1"


class ZenMuxBackend(_OpenAICompatBackend):
    """ZenMux -- aggregator; IDs use ``provider/model``."""

    DEFAULT_BASE_URL = "https://zenmux.ai/api/v1"


class ZAIBackend(GLMBackend):
    """Z.AI -- Zhipu's international platform (GLM model family).

    Same GLM envelope (``thinking_enabled`` + ``reasoning_content``
    extraction) as :class:`GLMBackend`, pointed at the international
    endpoint.  Coding-Plan subscribers must override ``base_url`` with
    ``https://api.z.ai/api/coding/paas/v4`` (official endpoint split).
    """

    DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "glm-5.3",
        **kwargs: Any,
    ) -> None:
        if not base_url:
            base_url = self.DEFAULT_BASE_URL
        super().__init__(api_key=api_key, base_url=base_url, model=model, **kwargs)


__all__ = [
    "AtomicChatBackend",
    "AzureCognitiveBackend",
    "AzureOpenAIBackend",
    "BasetenBackend",
    "CerebrasBackend",
    "CortecsBackend",
    "DeepInfraBackend",
    "FireworksBackend",
    "FrogBotBackend",
    "HeliconeBackend",
    "IONetBackend",
    "LlamaCppBackend",
    "NebiusBackend",
    "OVHCloudBackend",
    "OllamaCloudBackend",
    "OpenAI302Backend",
    "ScalewayBackend",
    "STACKITBackend",
    "TogetherBackend",
    "VeniceBackend",
    "VercelAIGatewayBackend",
    "XAIBackend",
    "ZAIBackend",
    "ZenMuxBackend",
]
