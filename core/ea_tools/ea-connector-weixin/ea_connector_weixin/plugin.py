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

"""Plugin registration for the ea-connector-weixin connector.

This module defines the ``_WeixinConnectorPlugin`` class, which extends
``EncrePlugin`` to declare its manifest metadata and register the Weixin
(WeChat) adapter spec through the S8 gateway-adapter surface
(``context.gateway_adapters.register``) at activation time.

The ``create_plugin`` factory is the entry point called by the plugin
loader. It returns a fully configured plugin instance without requiring
the caller to know about the internal ``_WeixinConnectorPlugin`` class.
"""

from __future__ import annotations

from encre.gateway.config import Platform
from encre.gateway.platform_registry import GatewayAdapterSpec
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource

from .weixin import WeixinAdapter, _check_requirements

#: Adapter spec bridged into the platform registry at activation.  Carries
#: ``plugin_name`` for ownership display and teardown filtering; the runner's
#: ``sync_plugin_adapters`` turns it into a first-class ``PlatformEntry``.
#: ``ui`` mirrors the desktop renderer's ADAPTER_DEFS entry so the connector
#: card renders without the renderer knowing about this plugin.
WEIXIN_ADAPTER_SPEC = GatewayAdapterSpec(
    name="weixin",
    label="WeChat",
    platform=Platform.WEIXIN,
    adapter_factory=lambda cfg: WeixinAdapter(cfg),
    check_fn=_check_requirements,
    required_env=["WEIXIN_BOT_TOKEN"],
    plugin_name="ea-connector-weixin",
    ui={
        "name": "WeChat",
        "desc": "After configuring iLink Bot, connect to WeChat to send and receive messages and auto-reply",
        "docs": "https://www.wechatbot.dev/zh/protocol",
        "qr": True,
        "fields": [],
    },
)


def create_plugin() -> EncrePlugin:
    """Instantiate and return the Weixin connector plugin.

    The plugin loader calls this factory to obtain a configured
    ``EncrePlugin`` instance. Wrapping the class in a factory isolates
    the constructor from import-time side effects and gives the loader
    a single, stable entry point regardless of how the internal class
    is structured.

    Returns:
        A configured ``_WeixinConnectorPlugin`` instance ready for
        registration with the Encre Agent plugin system.
    """
    return _WeixinConnectorPlugin()


class _WeixinConnectorPlugin(EncrePlugin):
    """Encre plugin that provides the Weixin (WeChat) gateway connector.

    This class is the implementation counterpart to ``create_plugin``.
    It inherits from ``EncrePlugin`` and supplies:

    * A ``manifest`` (class-level ``PluginManifest``) that describes the
      plugin's identity, capabilities, dependency graph, and safety
      classification, including the ``provides_gateways`` declaration and
      the ``gateways:register`` permission the S8 surface gates on.
    * An ``on_activate`` hook that registers :data:`WEIXIN_ADAPTER_SPEC`
      so the gateway runner bridges it into the platform registry.
    """

    manifest = PluginManifest(
        name="ea-connector-weixin",
        display_name="WeChat",
        icon="message-circle",
        version="0.5.0",
        description="EA (Encre Agent) system-default connector: WeChat (iLink Bot).",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "comms", "connector"],
        provides_gateways=["weixin"],
        activation_events=[],
        permissions=["gateways:register"],
    )

    def on_activate(self, context) -> None:
        """Register the adapter spec through the S8 gateway-adapter surface.

        The gateway runner's ``sync_plugin_adapters`` reads the host
        accumulator this populates and bridges the spec into the platform
        registry as a first-class ``PlatformEntry`` (``source="plugin"``).

        Args:
            context: The ``PluginContext`` handed over by the registry.
        """
        context.gateway_adapters.register(WEIXIN_ADAPTER_SPEC)
