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
Inspired by the Hermes Agent project (https://github.com/NousResearch/hermes-agent.git).
Thanks to Hermes Agent for the inspiration on this module.

Photon Spectrum (iMessage) platform package for Encre's IM gateway layer.

This package is the surface of the ``ea-connector-photon`` connector plugin.
Its adapter module, :mod:`ea_connector_photon.adapter`, implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter` through a small
supervised Node sidecar that runs the ``spectrum-ts`` SDK, and
:mod:`ea_connector_photon.auth` provides the on-disk project-credential store
used by ``encre photon setup``.

Registration is declarative: ``ea_connector_photon.plugin`` bridges a
``GatewayAdapterSpec`` into the platform registry at plugin activation, which
makes the ``photon`` platform discoverable to
:class:`encre.gateway.run.GatewayRunner` and to ``encre gateway status``.

Public API (re-exported for the platform registry):
    - :class:`PhotonAdapter` -- the adapter class registered for platform
      ``photon``.
    - :func:`check_requirements` -- dependency probe (httpx, Node binary,
      sidecar ``node_modules``).
    - :func:`validate_config` -- project-credential resolvability probe.
    - ``_build_adapter`` -- registry factory constructing
      :class:`PhotonAdapter`.
    - :func:`load_project_credentials` -- on-disk credential store reader.
"""

from .adapter import (
    PhotonAdapter,
    _build_adapter,
    check_requirements,
    validate_config,
)
from .auth import load_project_credentials

__all__ = [
    "PhotonAdapter",
    "_build_adapter",
    "check_requirements",
    "load_project_credentials",
    "validate_config",
]
