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

"""Gateway authorization: decide, per inbound platform message, whether the
sender is allowed to drive the agent.

This module is the security gate at the very front of the gateway pipeline.  It
runs inside :meth:`encre.gateway.platforms.base.BasePlatformAdapter.handle_message`,
upstream of session resolution and of the Rust agent core, so a rejected message
costs nothing beyond a lookup and never allocates an agent session.

The decision is a first-match-wins cascade:

0. **Nothing configured -> allow.** If no per-platform flag, no allowlist, no
   global flag, and no paired users exist, every sender is authorized.  This is
   the default so that a fresh deployment that has not configured any policy
   keeps working instead of silently locking everyone out.
1. **Per-platform allow-all** -- ``adapter_<name>_allow_all`` or
   ``<NAME>_ALLOW_ALL_USERS``.  Widest platform-scoped opt-in.
2. **Platform allowlist** -- ``adapter_<name>_allowed_users`` or
   ``<NAME>_ALLOWED_USERS``, a comma-separated list of platform user ids.  The
   message's ``user_id`` or ``user_id_alt`` must be present.
3. **DM pairing** -- the sender previously redeemed a code from
   :mod:`encre.gateway.pairing`, which binds ``(platform, user_id)`` to this
   gateway instance.
4. **Global allow-all** -- ``gateway_allow_all_users`` or
   ``GATEWAY_ALLOW_ALL_USERS``.
5. **Deny.**

Config sources: ``adapter_*`` / ``gateway_*`` keys in the encrypted settings
store (read through :func:`encre.settings_manager.load_settings`) plus
environment variables that override settings.  The env override is implemented
as an uppercase-of-the-key lookup in :meth:`AuthorizationChecker._cfg`, so the
same key name drives both layers without a second table.

Design contract: the check is stateless per call.  Settings and the pairing
store are re-read on every :meth:`AuthorizationChecker.is_authorized`, so an
operator flipping a flag or a user pairing a device takes effect on the next
message with no process restart.  The tradeoff is one settings read per inbound
message; that is acceptable because the read is a cached JSON load and is far
cheaper than the rejected path's alternative (running an agent turn).
"""

import logging
import os
from dataclasses import dataclass
from typing import Callable

from encre.gateway.pairing import PairingStore
from encre.gateway.session import SessionSource
from encre.settings_manager import load_settings

logger = logging.getLogger("encre.gateway.authz")

# Layer identifiers.  They are compared as opaque strings by callers (the runner
# logs them, the adapter may surface them in a reject notice), so they must stay
# stable even if the cascade order changes.  Note that LAYER_DEFAULT_ALLOW is
# emitted as the literal "default_allow" at its call site rather than as the
# constant, so both spellings must be kept in sync.
LAYER_PLATFORM_ALLOW_ALL = "platform_allow_all"
LAYER_ALLOWLIST = "allowlist"
LAYER_PAIRING = "pairing"
LAYER_GLOBAL_ALLOW_ALL = "global_allow_all"
LAYER_DENY = "deny"
LAYER_DEFAULT_ALLOW = "default_allow"

# Accepted spellings for a boolean config value.  YAML/env booleans arrive as
# strings in practice, so both numeric and word forms are accepted.
_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AuthzResult:
    """Outcome of one authorization decision.

    Frozen because results are constructed once per message and then only read
    -- by the adapter (to decide whether to reply with a reject notice) and by
    the logger (to attribute denials to a layer).  Freezing also makes a result
    safe to hand to telemetry.

    Attributes:
        authorized: Whether the sender may proceed to session resolution and
            agent execution.
        reason: One-line, log-safe explanation.  Never contains user content or
            tokens; it is safe to print at INFO/WARNING level.
        layer: The cascade step that decided, one of the ``LAYER_*`` constants.
    """

    authorized: bool
    reason: str
    layer: str


class AuthorizationChecker:
    """Stateless five-layer (plus default) authorization checker.

    Owned by :class:`encre.gateway.run.GatewayRunner`, which constructs one
    instance per gateway process and injects it into every adapter it starts
    via :meth:`encre.gateway.platforms.base.BasePlatformAdapter.set_authz`.
    Adapters call :meth:`is_authorized` synchronously on the incoming event.

    Lifecycle: construct -> call :meth:`is_authorized` on every inbound message
    -> discard.  There is no ``close()`` because the checker owns no resources;
    the settings reader and the :class:`PairingStore` it holds are owned
    elsewhere and are only referenced.

    Thread/async safety: all mutable state is immutable after ``__init__``
    (``_pairing`` and ``_config_fn`` are only read), and no cross-call caching
    exists, so the checker is safe to share across adapter threads and event
    loop callbacks.

    Args:
        pairing: Optional :class:`encre.gateway.pairing.PairingStore` used by
            the pairing layer.  ``None`` disables that layer entirely and makes
            the "anything configured" probe skip the paired-user count.
        config_fn: Zero-argument callable returning the raw settings mapping.
            Defaults to :func:`encre.settings_manager.load_settings`.  Injectable
            so tests and embedded deployments can supply their own source
            without monkeypatching the settings module.
    """

    def __init__(
        self,
        pairing: PairingStore | None = None,
        config_fn: Callable[[], dict] | None = None,
    ) -> None:
        """Capture the pairing store and the settings reader for later cascade steps.

        Stores the collaborators as-is without normalising or validating them,
        because :class:`AuthzResult` decisions are re-evaluated per inbound
        message from whatever the settings source returns at that moment.
        ``config_fn`` falls back to :func:`encre.settings_manager.load_settings`
        only when a falsy callable is supplied, which is what lets embedded and
        test callers inject a custom source by passing a real function.

        Invariant: the checker is fully populated before it is handed to an
        adapter by :meth:`encre.gateway.platforms.base.BasePlatformAdapter.set_authz`,
        so neither field is ever read as an unset default on the live inbound
        path. A ``None`` pairing store is legal and means the pairing layer is
        disabled rather than misconfigured.

        Args:
            pairing: Optional :class:`encre.gateway.pairing.PairingStore`;
                ``None`` disables the pairing layer.
            config_fn: Zero-argument settings provider, defaulting to
                :func:`encre.settings_manager.load_settings`.

        Returns:
            ``None``; the checker is ready to answer :meth:`is_authorized`.
        """
        self._pairing = pairing
        self._config_fn = config_fn or load_settings

    def _settings(self) -> dict:
        """Return the current settings mapping, or an empty mapping on failure.

        Swallowing every exception is deliberate: authorization must never turn
        a settings-store error (corrupt file, missing key, IO failure) into a
        crash that stops all platforms.  A failed read degrades to "no settings",
        which the cascade treats as an unconfigured system.

        Returns:
            The settings dict, or ``{}``.
        """
        try:
            return self._config_fn() or {}
        except Exception:
            return {}

    @staticmethod
    def _truthy(value: str | None) -> bool:
        """Return ``True`` for a config value in the accepted true spellings.

        ``None`` and the empty string are false even though ``bool("")`` is false
        anyway; the explicit check keeps intent visible for the callers that
        pass settings values of unknown type.

        Args:
            value: Raw string (or ``None``) from settings or the environment.

        Returns:
            Whether the value means "enabled".
        """
        return bool(value) and str(value).strip().lower() in _TRUE

    @staticmethod
    def _split_ids(raw: str) -> list[str]:
        """Split a comma-separated allowlist into trimmed, non-empty ids.

        Allows ``"a, b, c"`` and ``"a,b,,c"`` shapes from YAML or the shell
        without a malformed entry accidentally matching a blank user id.

        Args:
            raw: The allowlist string as stored in settings or env.

        Returns:
            Trimmed user ids, empty-string entries dropped.
        """
        return [s.strip() for s in str(raw).split(",") if s.strip()]

    def _cfg(self, key: str, default: str = "") -> str:
        """Read a config value with environment override.

        The environment key is the settings key uppercased, so
        ``adapter_telegram_allow_all`` is overridden by
        ``ADAPTER_TELEGRAM_ALLOW_ALL`` and ``gateway_allow_all_users`` by
        ``GATEWAY_ALLOW_ALL_USERS``.  This gives operators a single deployment
        knob style for the whole gateway.

        Args:
            key: Lowercase settings key.
            default: Returned when neither env nor settings provide a value.

        Returns:
            The resolved string value.
        """
        env = os.environ.get(key.upper())
        if env is not None and env != "":
            return env
        return str(self._settings().get(key, default))

    def _any_auth_configured(self, adapter_name: str, upper: str) -> bool:
        """Return whether any authorization policy is in effect.

        This is the layer-0 probe.  Without it a brand-new installation would
        fall through the whole cascade to the deny branch and reject every user,
        which is the wrong default: no policy means no gate.

        The probe is intentionally wider than the cascade itself -- it also
        counts a non-empty :class:`encre.gateway.pairing.PairingStore` -- so
        that pairing alone is enough to switch authorization on.

        Args:
            adapter_name: Lowercase adapter/platform id, e.g. ``"telegram"``.
            upper: ``adapter_name.upper()``, precomputed by the caller to avoid
                re-allocating the string on every message.

        Returns:
            ``True`` when at least one policy source is present.
        """
        # Per-platform settings
        if self._truthy(self._cfg(f"adapter_{adapter_name}_allow_all")):
            return True
        if self._cfg(f"adapter_{adapter_name}_allowed_users"):
            return True
        # Per-platform env vars
        if self._truthy(os.environ.get(f"{upper}_ALLOW_ALL_USERS")):
            return True
        if os.environ.get(f"{upper}_ALLOWED_USERS"):
            return True
        # Global settings/env
        if self._truthy(self._cfg("gateway_allow_all_users")):
            return True
        if self._truthy(os.environ.get("GATEWAY_ALLOW_ALL_USERS")):
            return True
        # Pairing store with at least one paired user
        if self._pairing is not None and self._pairing.list_paired():
            return True
        return False

    def is_authorized(self, source: SessionSource, adapter_name: str) -> AuthzResult:
        """Evaluate the authorization cascade for one inbound message.

        The layers are ordered by intent specificity: the narrowest opt-in
        (a per-platform allow-all) is checked before the broadest one (global
        allow-all), so that a deployment that enables both records the reason it
        actually matched rather than a generic one.  The global flag comes
        before deny but after pairing, which keeps pairing observable in logs
        even in a globally-open deployment.

        Args:
            source: Structured origin of the message, as produced by
                :mod:`encre.gateway.session`.  Only ``user_id``,
                ``user_id_alt`` and ``platform`` are consumed here.
            adapter_name: The adapter's ``name`` (platform id, lowercase).

        Returns:
            An :class:`AuthzResult` describing the first matching layer.

        Note:
            Both ``user_id`` and ``user_id_alt`` are considered for allowlist
            and pairing.  ``user_id_alt`` exists because several vendors expose
            a second stable identifier (e.g. an internal numeric id alongside a
            visible name), and requiring only one of them to match keeps those
            adapters from having to guess which id operators will whitelist.
        """
        upper = adapter_name.upper()
        user_ids = {source.user_id, source.user_id_alt}
        user_ids.discard(None)

        # Layer 0: no policy anywhere -> open gate, matching the pre-authorization
        # behaviour so existing deployments are not locked out on upgrade.
        if not self._any_auth_configured(adapter_name, upper):
            return AuthzResult(True, "no auth configured", "default_allow")

        # 1. Per-platform allow-all.
        if self._truthy(self._cfg(f"adapter_{adapter_name}_allow_all")):
            return AuthzResult(True, f"platform {adapter_name} allow-all", LAYER_PLATFORM_ALLOW_ALL)
        env_allow_all = os.environ.get(f"{upper}_ALLOW_ALL_USERS")
        if self._truthy(env_allow_all):
            return AuthzResult(True, f"platform {adapter_name} allow-all (env)", LAYER_PLATFORM_ALLOW_ALL)

        # 2. Platform allowlist.  Settings win over env here: the settings value
        # is preferred when both are set, so an operator can narrow a broad env
        # allowlist per platform without removing the env var.
        allowed_raw = self._cfg(f"adapter_{adapter_name}_allowed_users") or os.environ.get(
            f"{upper}_ALLOWED_USERS", ""
        )
        if allowed_raw:
            allowed = set(self._split_ids(allowed_raw))
            matched = next((u for u in user_ids if u and u in allowed), None)
            if matched is not None:
                return AuthzResult(True, f"allowlist ({adapter_name})", LAYER_ALLOWLIST)

        # 3. DM pairing.  Iterating both ids makes pairing work for adapters
        # that only populate one of the two user-id fields.
        if self._pairing is not None:
            for uid in user_ids:
                if uid and self._pairing.is_paired(source.platform, uid):
                    return AuthzResult(True, "paired", LAYER_PAIRING)

        # 4. Global allow-all.
        if self._truthy(self._cfg("gateway_allow_all_users")):
            return AuthzResult(True, "global allow-all", LAYER_GLOBAL_ALLOW_ALL)
        if self._truthy(os.environ.get("GATEWAY_ALLOW_ALL_USERS")):
            return AuthzResult(True, "global allow-all (env)", LAYER_GLOBAL_ALLOW_ALL)

        # 5. Default: deny.
        return AuthzResult(False, "not authorized", LAYER_DENY)
