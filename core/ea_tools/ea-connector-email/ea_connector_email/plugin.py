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

"""Plugin registration for the ea-connector-email connector.

This module defines the ``_EmailConnectorPlugin`` class, which extends
``EncrePlugin`` to declare its manifest metadata and register the Email
adapter spec through the S8 gateway-adapter surface
(``context.gateway_adapters.register``) at activation time.

The ``create_plugin`` factory is the entry point called by the plugin
loader. It returns a fully configured plugin instance without requiring
the caller to know about the internal ``_EmailConnectorPlugin`` class.
"""

from __future__ import annotations

from encre.gateway.config import Platform
from encre.gateway.platform_registry import GatewayAdapterSpec
from encre.plugins.types import EncrePlugin, PluginManifest, PluginSource

from .adapter import (
    MAX_MESSAGE_LENGTH,
    _build_adapter,
    _check_requirements,
)

#: Declarative connector-card definition consumed by the desktop renderer.
#: ``labelKey``/``placeholderKey`` stay renderer-side locale keys so
#: translations remain centralized; ``name``/``desc`` are fallbacks used
#: when no locale entry exists. Mirrors the ``email`` entry of the
#: renderer's ``ADAPTER_DEFS`` table.
EMAIL_UI = {
    "name": "Email",
    "desc": (
        "Send and receive emails via SMTP/IMAP, with auto-reply and "
        "processing support"
    ),
    "docs": "",
    "qr": False,
    "fields": [
        {
            "key": "smtp_host",
            "labelKey": "fieldSmtpHost",
            "type": "text",
            "placeholderKey": "placeholderSmtpHost",
        },
        {
            "key": "smtp_port",
            "labelKey": "fieldSmtpPort",
            "type": "number",
            "placeholderKey": "placeholderSmtpPort",
        },
        {
            "key": "smtp_user",
            "labelKey": "fieldSmtpUser",
            "type": "text",
            "placeholderKey": "placeholderSmtpUser",
        },
        {
            "key": "smtp_pass",
            "labelKey": "fieldSmtpPass",
            "type": "password",
            "placeholderKey": "placeholderSmtpPass",
        },
        {
            "key": "imap_host",
            "labelKey": "fieldImapHost",
            "type": "text",
            "placeholderKey": "placeholderImapHost",
        },
        {
            "key": "imap_port",
            "labelKey": "fieldImapPort",
            "type": "number",
            "placeholderKey": "placeholderImapPort",
        },
    ],
}

#: Adapter spec bridged into the platform registry at activation.  Carries
#: ``plugin_name`` for ownership display and teardown filtering; the runner's
#: ``sync_plugin_adapters`` turns it into a first-class ``PlatformEntry``.
EMAIL_ADAPTER_SPEC = GatewayAdapterSpec(
    name="email",
    label="Email",
    platform=Platform.EMAIL,
    adapter_factory=_build_adapter,
    check_fn=_check_requirements,
    required_env=["EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_SMTP_HOST"],
    max_message_length=MAX_MESSAGE_LENGTH,
    emoji="📧",
    plugin_name="ea-connector-email",
    ui=EMAIL_UI,
)


def create_plugin() -> EncrePlugin:
    """Instantiate and return the Email connector plugin.

    The plugin loader calls this factory to obtain a configured
    ``EncrePlugin`` instance. Wrapping the class in a factory isolates
    the constructor from import-time side effects and gives the loader
    a single, stable entry point regardless of how the internal class
    is structured.

    Returns:
        A configured ``_EmailConnectorPlugin`` instance ready for registration
        with the Encre Agent plugin system.
    """
    return _EmailConnectorPlugin()


class _EmailConnectorPlugin(EncrePlugin):
    """Encre plugin that provides the Email gateway connector.

    This class is the implementation counterpart to ``create_plugin``.
    It inherits from ``EncrePlugin`` and supplies:

    * A ``manifest`` (class-level ``PluginManifest``) that describes the
      plugin's identity, capabilities, dependency graph, and safety
      classification, including the ``provides_gateways`` declaration and
      the ``gateways:register`` permission the S8 surface gates on.
    * An ``on_activate`` hook that registers :data:`EMAIL_ADAPTER_SPEC` so
      the gateway runner bridges it into the platform registry.

    The adapter itself is standard-library only (``imaplib`` / ``smtplib``),
    so no optional dependency extras are required. The environment probe
    :func:`~.adapter.check_email_requirements` stays available on the
    package surface for setup UIs that want a credential-completeness
    verdict beyond :func:`_check_requirements`.
    """

    manifest = PluginManifest(
        name="ea-connector-email",
        display_name="Email",
        icon="mail",
        version="0.5.0",
        description="EA (Encre Agent) system-default connector: Email.",
        author="Dunimd Team <dunimd@outlook.com>",
        license="Apache-2.0",
        homepage="https://github.com/mf2023/Encre",
        source=PluginSource.BUNDLED,
        tier="system-default",
        dependencies=[],
        engines={"encre": ">=0.5"},
        tags=["system-default", "comms", "connector"],
        provides_gateways=["email"],
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
        context.gateway_adapters.register(EMAIL_ADAPTER_SPEC)
