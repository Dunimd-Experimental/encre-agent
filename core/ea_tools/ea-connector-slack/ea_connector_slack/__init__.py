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

"""Slack platform package: flat re-export surface for the Slack adapter.

Attribution: inspired by the Hermes Agent project
(https://github.com/NousResearch/hermes-agent.git).

This module is the package surface of the ea-connector-slack connector
plugin. It exposes every public symbol of the Slack package at a single
import level so callers can write
``from ea_connector_slack import SlackAdapter`` without naming a sub-module.

Ownership and data flow:

* :class:`SlackAdapter` implements
  :class:`encre.gateway.platforms.base.BasePlatformAdapter` and is
  instantiated, started, and torn down by
  :class:`encre.gateway.run.GatewayRunner`.
* Inbound path: Slack Socket Mode (slack-bolt) dispatch -> adapter event
  handler -> :class:`encre.gateway.platforms.base.MessageEvent` ->
  :class:`encre.gateway.run.GatewayRunner` (authorization, session
  resolution) -> Encre Agent Core agent.
* Outbound path: agent reply ->
  :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` -> adapter
  :meth:`~encre.gateway.platforms.base.BasePlatformAdapter.send` -> Slack
  Web API (``chat.postMessage``) as Block Kit / mrkdwn messages.

Sub-modules re-exported here:

* ``adapter`` -- the adapter: Socket Mode connect/resume, event dispatch,
  slash-command handling, threaded conversations, ACL gating, Web API send,
  and the setup / YAML-config / standalone-send hook implementations.
* ``block_kit`` -- markdown-to-Block-Kit renderer used for opt-in rich
  outbound messages (``slack.extra.rich_blocks``).

Configuration: set ``SLACK_BOT_TOKEN`` and ``SLACK_APP_TOKEN`` in the
environment, or place tokens under the ``slack`` platform section of
``config.yaml``.

Public API:
    :class:`SlackAdapter`, :func:`check_slack_requirements`,
    :data:`SLACK_AVAILABLE`, :func:`interactive_setup`,
    :func:`render_blocks`.

Reference: https://docs.slack.dev/
"""

# Adapter surface: the platform class, the dependency checker consumed by
# the gateway-adapter spec, the availability flag, and the ``encre setup``
# wizard hook.
from .adapter import (  # noqa: F401
    SLACK_AVAILABLE,
    SlackAdapter,
    check_slack_requirements,
    interactive_setup,
)

# Rich-render surface: pure markdown -> Block Kit renderer; returns ``None``
# when the content cannot be expressed within Slack's limits so the caller
# falls back to plain mrkdwn.
from .block_kit import render_blocks  # noqa: F401

# Explicit public surface for ``dir()`` and star-import consumers.
__all__ = [
    # adapter
    "SLACK_AVAILABLE",
    "SlackAdapter",
    "check_slack_requirements",
    "interactive_setup",
    # block_kit
    "render_blocks",
]
