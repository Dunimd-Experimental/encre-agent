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
Discord platform package for the Encre gateway layer.

This package hosts Encre's Discord integration built on top of the third-party
``discord.py`` library.  It is one of the IM-adapter packages under
``encre.gateway.platforms``: :class:`encre.gateway.run.GatewayRunner` constructs
exactly one adapter instance per configured Discord profile, drives its
``connect()`` / ``send()`` / ``disconnect()`` lifecycle, and feeds the
:class:`~encre.gateway.platforms.base.MessageEvent` values the adapter produces
into the agent core while relaying agent output back through the adapter's
:class:`~encre.gateway.platforms.base.SendResult` surface.

Public API re-exported here:

* ``adapter`` -- the implementation module.  It defines
  :class:`ea_connector_discord.adapter.DiscordAdapter` and the plugin hooks
  (``interactive_setup``, ``_apply_yaml_config``, ``_is_connected`` and
  ``_build_adapter``) consumed by the ea-connector-discord connector plugin.
  Importing this package no longer mutates the platform registry: the
  ``discord`` adapter is bridged into
  :data:`encre.gateway.platform_registry.platform_registry` at plugin
  activation time through the ``GatewayAdapterSpec`` declared in
  ``plugin.py``.

The sibling modules :mod:`ea_connector_discord.recovery` (durable
SQLite ledger for missed-message recovery) and
:mod:`ea_connector_discord.voice_mixer` (continuous voice audio
mixer) are imported lazily from inside ``adapter`` rather than re-exported here,
so that a partial or missing optional dependency (``numpy`` for the voice mixer)
cannot break platform discovery.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).  Thanks to Hermes Agent for
the inspiration on this module.
"""

from . import adapter
