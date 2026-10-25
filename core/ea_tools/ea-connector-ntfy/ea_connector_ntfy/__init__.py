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
ntfy platform package for Encre's IM gateway layer.

This package is the surface of the ``ea-connector-ntfy`` connector plugin.
Its single adapter module, :mod:`ea_connector_ntfy.adapter`, implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter` on top of an ntfy
topic. Inbound traffic arrives over a long-lived
``GET {server}/{topic}/json?poll=false`` streaming connection that a
background task decodes line by line into
:class:`~encre.gateway.platforms.base.MessageEvent` objects; outbound traffic
is ``POST {server}/{publish_topic}``, whose
:class:`~encre.gateway.platforms.base.SendResult` values are consumed by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`. Only ``httpx``
is required, so the adapter stays available on runtimes without an ntfy SDK.

Registration is declarative: ``ea_connector_ntfy.plugin`` bridges a
``GatewayAdapterSpec`` into the platform registry at plugin activation, which
makes the ``ntfy`` platform discoverable to
:class:`encre.gateway.run.GatewayRunner`, to ``encre gateway status``, and to
the cron / send-message standalone-send paths.

ntfy has no authenticated user identity and no topic metadata, so the adapter
treats a topic as a single trusted channel and fixes ``user_id`` to the topic
name. A real trust boundary requires a topic read token supplied through
``NTFY_TOKEN`` or ``config.yaml`` ``platforms.ntfy.extra["token"]``.

Public API (re-exported for the platform registry):
    - :class:`NtfyAdapter` -- the adapter class registered for platform
      ``ntfy``.
    - :data:`MAX_MESSAGE_LENGTH` -- hard cap on a published message body.
    - :func:`check_requirements` -- installability and minimal-config probe.
    - :func:`validate_config` -- config-shape probe.
    - ``_build_adapter`` -- registry factory constructing :class:`NtfyAdapter`.
"""

from .adapter import (
    MAX_MESSAGE_LENGTH,
    NtfyAdapter,
    _build_adapter,
    check_requirements,
    validate_config,
)

__all__ = [
    "MAX_MESSAGE_LENGTH",
    "NtfyAdapter",
    "_build_adapter",
    "check_requirements",
    "validate_config",
]
