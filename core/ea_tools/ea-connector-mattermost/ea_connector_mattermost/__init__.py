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
Mattermost platform package for Encre's IM gateway layer.

This package is the surface of the ``ea-connector-mattermost`` connector
plugin. Its single adapter module, :mod:`ea_connector_mattermost.adapter`,
implements :class:`encre.gateway.platforms.base.BasePlatformAdapter` against
the Mattermost REST API. Inbound traffic is collected by the ``POST /hooks``
webhook handler, verified with the incoming-webhook token, and converted into
:class:`~encre.gateway.platforms.base.MessageEvent` objects; outbound traffic
is posted through ``POST /api/v4/posts`` (with ``/files`` uploads for media),
whose :class:`~encre.gateway.platforms.base.SendResult` values are consumed by
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer`. Only ``httpx``
is required, so the adapter stays available on runtimes without a Mattermost
SDK.

Registration is declarative: ``ea_connector_mattermost.plugin`` bridges a
``GatewayAdapterSpec`` into the platform registry at plugin activation, which
makes the ``mattermost`` platform discoverable to
:class:`encre.gateway.run.GatewayRunner`, to ``encre gateway status``, and to
the cron / send-message standalone-send paths.

Credentials are read from ``MATTERMOST_TOKEN`` and ``MATTERMOST_URL``
(plus ``MATTERMOST_ALLOWED_USERS`` and ``MATTERMOST_HOME_CHANNEL``) or from
``config.yaml`` ``platforms.mattermost.extra``. Channel membership is enforced
with the ``allowed_channels`` list, and replies are threaded when
``thread_id`` is present in the outgoing metadata.

Public API (re-exported for the platform registry):
    - :class:`MattermostAdapter` -- the adapter class registered for platform
      ``mattermost``.
    - :func:`check_mattermost_requirements` -- runtime dependency probe.
    - :func:`validate_mattermost_config` -- config-shape probe.
    - ``_build_adapter`` -- registry factory constructing
      :class:`MattermostAdapter`.
"""

from .adapter import (
    MattermostAdapter,
    _build_adapter,
    check_mattermost_requirements,
    validate_mattermost_config,
)

__all__ = [
    "MattermostAdapter",
    "_build_adapter",
    "check_mattermost_requirements",
    "validate_mattermost_config",
]
