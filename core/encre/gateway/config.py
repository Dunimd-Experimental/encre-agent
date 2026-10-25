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

"""Gateway configuration model and loader.

This module defines the typed shape of gateway configuration and resolves it
from two sources, with the environment always winning:

1. The encrypted settings store, read through
   :func:`encre.settings_manager.load_settings`, which holds ``adapter_*`` and
   ``gateway_*`` keys written by the desktop UI, the pairing flow, and the
   per-platform ``save_env_value`` helpers.
2. Process environment variables, applied last via
   :func:`_apply_env_overrides`.

Position in the gateway: :class:`encre.gateway.run.GatewayRunner` calls
:func:`load_gateway_config` once at startup.  The resulting
:class:`GatewayConfig` is then sliced per adapter -- each platform adapter
receives its own :class:`PlatformConfig` and reads whatever it needs out of
``extra`` -- plus the global flags and streaming defaults that the runner and
:class:`encre.gateway.stream_consumer.GatewayStreamConsumer` consume directly.

Two layers of platform discovery coexist:

* Explicit: settings contain ``adapter_<name>_enabled`` / ``adapter_<name>_token``.
* Implicit: an env var from :data:`_ENV_TOKEN_MAP` auto-enables the platform.
  This lets a deployment bring a platform online with a single environment
  variable and no UI round-trip.

Public API:
    * :class:`Platform` -- enumeration of every supported platform id.
    * :class:`PlatformConfig` / :class:`HomeChannel` -- per-platform shape.
    * :class:`GatewayConfig` -- the container the runner owns.
    * :func:`load_gateway_config` -- the only entry point for building one.
"""

import enum
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("encre.gateway.config")


# ---- Platform enumeration ---------------------------------------------------


class Platform(enum.Enum):
    """Every messaging platform the gateway can bridge.

    The member *value* is the canonical platform id: it is the key used for
    ``adapter_<value>_`` settings prefixes, the name adapters report via
    :attr:`encre.gateway.platforms.base.BasePlatformAdapter.name`, the platform
    id persisted in :class:`encre.gateway.session.SessionSource`, and the key
    used by :class:`encre.gateway.platform_registry.PlatformRegistry`.  Changing
    a value is therefore a breaking change across config, session history, and
    pairing state, not just a rename.

    Note that not every value matches the vendor's marketing name: ``QQ`` is
    ``"qqbot"`` because that is the adapter directory and bot api id, while
    ``WEIXIN``, ``FEISHU`` and ``WECOM`` are distinct integrations.
    """

    TELEGRAM = "telegram"
    DISCORD = "discord"
    QQ = "qqbot"
    FEISHU = "feishu"
    DINGTALK = "dingtalk"
    WEIXIN = "weixin"
    WECOM = "wecom"
    SLACK = "slack"
    SIGNAL = "signal"
    MATRIX = "matrix"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    HOMEASSISTANT = "homeassistant"
    MSGRAPH = "msgraph"
    SMS = "sms"
    WEBHOOK = "webhook"
    BLUEBUBBLES = "bluebubbles"
    API_SERVER = "api_server"
    GOOGLE_CHAT = "google_chat"
    IRC = "irc"
    LINE = "line"
    MATTERMOST = "mattermost"
    NTFY = "ntfy"
    PHOTON = "photon"
    RAFT = "raft"
    SIMPLEX = "simplex"
    TEAMS = "teams"
    RELAY = "relay"


# ---- Per-platform configuration --------------------------------------------


@dataclass
class PlatformConfig:
    """Configuration for one platform adapter.

    This is a deliberately narrow container: two well-known scalars and an open
    dict.  Adapters differ enormously in what they need (bot tokens, app ids,
    webhook urls, allowed users, signal cli paths, phone numbers, device
    stores), so anything beyond ``enabled`` and ``token`` lives in ``extra`` and
    is read by the adapter itself.  Keeping the loader vendor-agnostic is what
    allows adding a platform without touching this module.

    Attributes:
        enabled: Whether the adapter should be started by the runner.
        token: Primary credential.  Some vendors repurpose this field for a
            non-token value (see :data:`_ENV_TOKEN_MAP`); adapters know their
            own convention.
        extra: Platform-specific keys, everything left over after ``enabled``
            and ``token`` are extracted.  Adapters read their fields from here.
        home_channel: Default delivery destination for cron and notification
            output.  Not populated by :func:`load_gateway_config`; adapters that
            use it receive it from the runner or from their own seeding flow.
    """

    enabled: bool = False
    token: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    home_channel: Optional["HomeChannel"] = None


@dataclass
class HomeChannel:
    """Default delivery channel for cron and notification output on one platform.

    When an agent run produces output outside a live conversation (a cron job,
    a heartbeat, a background result), the gateway needs a place to send it.
    This records that place.  Resolution order in the delivery path is: explicit
    target, then this home channel, then nothing (dropped with a warning).

    Attributes:
        chat_id: The target chat/channel/group id, in the id form the platform
            adapter's ``send()`` expects.
        name: Human-friendly display name, for UI and log legibility only.
        thread_id: Thread or topic id within the chat; ``None`` for the top
            level.  Needed by platforms that namespace messages inside a chat.
    """

    chat_id: str = ""
    name: str = ""
    thread_id: str | None = None


# ---- Top-level gateway configuration ----------------------------------------


@dataclass
class GatewayConfig:
    """Top-level gateway configuration container.

    Owned by :class:`encre.gateway.run.GatewayRunner` and read in three places:
    the runner (which platforms to start, global flags), the adapters (their
    own :class:`PlatformConfig` slice), and the stream consumer (streaming
    defaults).  The mappings are keyed by the platform *name string* (the
    :class:`Platform` member value for built-ins, the spec ``name`` for
    plugin-registered connectors), so iterating the config covers built-in
    and plugin platforms uniformly.

    Attributes:
        platforms: Per-platform configs.  Only platforms the loader found
            settings or env vars for are present.
        home_channels: Per-platform home channels.  Not populated by
            :func:`load_gateway_config`; kept on the container so callers can
            attach them from their own source.
        allow_all_users: Global authorization override; the last allow layer
            in :class:`encre.gateway.authz.AuthorizationChecker`.
        require_mention: Whether group messages must mention the bot to be
            processed.  Consulted by adapters and the runner's group guard.
        multiplex_profiles: Whether a single platform connection is shared
            across agent profiles.  Not read by this loader; callers set it
            directly.
        streaming_edit_interval: Seconds between in-place message edits while
            relaying an agent turn.
        streaming_buffer_threshold: Characters of accumulated output before an
            edit is issued.
        streaming_cursor: Text appended to an in-flight message so the user can
            see it is still being generated.
    """

    platforms: dict[str, PlatformConfig] = field(default_factory=dict)
    home_channels: dict[str, HomeChannel] = field(default_factory=dict)

    # Global gateway flags
    allow_all_users: bool = False
    require_mention: bool = False
    multiplex_profiles: bool = False

    # Streaming defaults
    streaming_edit_interval: float = 1.0
    streaming_buffer_threshold: int = 50
    streaming_cursor: str = " ..."


# ---- Defaults ---------------------------------------------------------------


DEFAULT_STREAMING_EDIT_INTERVAL = 1.0
DEFAULT_STREAMING_BUFFER_THRESHOLD = 50
DEFAULT_STREAMING_CURSOR = " ..."


# ---- Config loading ---------------------------------------------------------


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce a loosely-typed config value to a boolean.

    The settings store is encrypted JSON, so YAML booleans, JSON booleans, and
    env-var strings can all arrive here.  Only the string spellings in
    ``{"true", "1", "yes", "on"}`` are true; anything else non-``None`` falls
    back to Python truthiness, which keeps numeric and list values working.

    Args:
        value: Raw value from settings or the environment.
        default: Returned when ``value`` is ``None``.

    Returns:
        The coerced boolean.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def load_gateway_config() -> GatewayConfig:
    """Load gateway configuration from settings plus environment overrides.

    Precedence, lowest to highest: dataclass defaults -> settings store ->
    environment.  A settings read failure degrades to an empty settings mapping
    rather than raising, so a corrupt settings file still yields a usable
    config (with env-only platforms enabled).

    Args: none.

    Returns:
        A fully resolved :class:`GatewayConfig`.  Only platforms with at least
        one ``adapter_<name>_`` settings key, or one of the :data:`_ENV_TOKEN_MAP`
        env vars set, are present in :attr:`GatewayConfig.platforms`.
    """
    config = GatewayConfig()

    # Load from settings store
    try:
        from encre.settings_manager import load_settings
        settings = load_settings() or {}
    except Exception:
        settings = {}

    # Global flags
    config.allow_all_users = _coerce_bool(
        os.getenv("GATEWAY_ALLOW_ALL_USERS") or settings.get("gateway_allow_all_users")
    )
    config.require_mention = _coerce_bool(
        os.getenv("GATEWAY_REQUIRE_MENTION") or settings.get("gateway_require_mention")
    )

    # Per-platform: extract from settings (adapter_<name>_* keys)
    for plat in Platform:
        name = plat.value
        prefix = f"adapter_{name}_"
        plat_settings = {
            k[len(prefix):]: v
            for k, v in settings.items()
            if k.startswith(prefix)
        }
        if not plat_settings:
            continue

        pc = PlatformConfig(
            enabled=_coerce_bool(plat_settings.pop("enabled", None)),
            token=str(plat_settings.pop("token", "") or ""),
            extra=plat_settings,
        )
        config.platforms[name] = pc

    # Plugin-registered connectors: same adapter_<name>_ extraction keyed by
    # the bridged entry name.  The import is deferred because
    # encre.gateway.platform_registry imports this module at load time; by the
    # time this function runs the registry module is fully initialised.
    try:
        from encre.gateway.platform_registry import platform_registry
        for entry in platform_registry.plugin_entries():
            prefix = f"adapter_{entry.name}_"
            plat_settings = {
                k[len(prefix):]: v
                for k, v in settings.items()
                if k.startswith(prefix)
            }
            if not plat_settings:
                continue
            pc = PlatformConfig(
                enabled=_coerce_bool(plat_settings.pop("enabled", None)),
                token=str(plat_settings.pop("token", "") or ""),
                extra=plat_settings,
            )
            config.platforms[entry.name] = pc
    except Exception as e:
        logger.warning("[gateway] plugin platform config load failed (non-fatal): %s", e)

    # Environment variable overrides for common platforms
    _apply_env_overrides(config)

    return config


def _apply_env_overrides(config: GatewayConfig) -> None:
    """Auto-enable platforms whose credential is present in the environment.

    Mutates ``config`` in place.  This is what makes a headless deployment
    workable: exporting one variable brings a platform online without ever
    touching the settings store, which matters for containers and CI where the
    UI has not been run yet.

    The mapping is defined locally rather than at module scope so that the env
    contract lives next to the code that consumes it.  Note that two entries are
    not tokens despite the map name: signal supplies a CLI path and whatsapp a
    phone number.  Both are stored in :attr:`PlatformConfig.token`, the only
    scalar field this loader is guaranteed to populate, and the corresponding
    adapters read their value from there.

    Args:
        config: The config being built; platform entries are created if absent.
    """
    _ENV_TOKEN_MAP: dict[Platform, str] = {
        Platform.TELEGRAM: "TELEGRAM_BOT_TOKEN",
        Platform.DISCORD: "DISCORD_BOT_TOKEN",
        Platform.SLACK: "SLACK_BOT_TOKEN",
        Platform.SIGNAL: "SIGNAL_CLI_PATH",
        Platform.WHATSAPP: "WHATSAPP_PHONE_NUMBER",
        Platform.MATRIX: "MATRIX_ACCESS_TOKEN",
    }

    for plat, env_var in _ENV_TOKEN_MAP.items():
        token = os.getenv(env_var, "").strip()
        if token:
            pname = plat.value
            if pname not in config.platforms:
                config.platforms[pname] = PlatformConfig()
            config.platforms[pname].enabled = True
            config.platforms[pname].token = token
