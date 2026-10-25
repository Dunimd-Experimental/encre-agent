#!/usr/bin/env python3

# Copyright © 2025-2026 Wenze Wei. All Rights Reserved.
#
# This file is part of Encre.
# The Encre project belongs to the Dunimd Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
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

"""Central configuration model for Encre.

Defines the dataclasses that describe an Encre session -- the top-level
:class:`EncreConfig`, per-model :class:`ModelConfig`, sub-agent and
agent configs, and the serialisation helpers that read/write the encrypted
configuration under the data directory. Also provides the ``ThinkingConfig``
union (de)serialisation shared with other modules.

Persistence is delegated to :mod:`encre.config_store`, which splits the
configuration across one file per responsibility (models / agents /
adapters / mcp / ui / runtime) instead of dumping the whole blob into what
used to be called the "model config file".  The public API here --
:meth:`EncreConfig.from_file` and :meth:`EncreConfig.save` -- is unchanged.
"""

import contextlib
import os
import random
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from encre import config_store
from encre.crypto import decrypt, encrypt
from encre.paths import get_data_dir
from encre.utils.types import (
    AdaptiveThinking,
    DisabledThinking,
    EnabledThinking,
    PermissionMode,
    ThinkingConfig,
)

__all__ = [
    "AgentConfig",
    "EncreConfig",
    "ModelConfig",
    "SubAgentConfig",
    "get_data_dir",
    "get_env_value",
    "load_env_store",
    "save_env_value",
]


def _thinking_config_to_dict(config: ThinkingConfig | None) -> dict[str, Any] | None:
    """Serialize a ThinkingConfig union into a plain dictionary for TOML storage.

    Strips fields that hold their default values so the persisted config
    stays compact and deterministic across restarts.  The ``type`` field
    is always included to disambiguate the three union variants on read.

    Args:
        config: The :class:`ThinkingConfig` instance to serialize, or ``None``.

    Returns:
        A flat dictionary suitable for JSON/TOML serialisation, or ``None``
        when *config* is ``None``.
    """
    if config is None:
        return None
    if isinstance(config, AdaptiveThinking):
        return {
            "type": "adaptive",
            **{
                k: v
                for k, v in {
                    "enabled": config.enabled,
                    "min_tokens": config.min_tokens,
                    "max_tokens": config.max_tokens,
                    "budget_ratio": config.budget_ratio,
                    "level": config.level,
                }.items()
                if v or k == "enabled"
            },
        }
    if isinstance(config, EnabledThinking):
        return {
            "type": "enabled",
            **{
                k: v
                for k, v in {
                    "enabled": config.enabled,
                    "budget_tokens": config.budget_tokens,
                    "level": config.level,
                    "selectable": config.selectable,
                }.items()
                if v or k in ("enabled", "selectable")
            },
        }
    if isinstance(config, DisabledThinking):
        return {"type": "disabled", "enabled": config.enabled}
    return None


def _thinking_config_from_dict(d: Any) -> ThinkingConfig | None:
    """Deserialize a plain dictionary back into a ``ThinkingConfig`` union.

    Dispatches on the ``type`` key (``"adaptive"``, ``"enabled"``,
    ``"disabled"``) and falls back to sensible defaults for missing
    fields so old config files that predate newer variants continue to
    load without crashing.

    Args:
        d: A dictionary read from the encrypted config TOML, typically
            the value of the ``thinking_config`` key.

    Returns:
        The appropriate :class:`ThinkingConfig` subclass instance, or
        ``None`` when *d* is not a dictionary.
    """
    if not isinstance(d, dict):
        return None
    kind = str(d.get("type", "")).lower()
    if kind == "adaptive":
        return AdaptiveThinking(
            enabled=bool(d.get("enabled", True)),
            min_tokens=int(d.get("min_tokens", 1024)),
            max_tokens=int(d.get("max_tokens", 8192)),
            budget_ratio=float(d.get("budget_ratio", 0.5)),
            level=str(d.get("level", "")),
        )
    if kind == "enabled":
        return EnabledThinking(
            enabled=bool(d.get("enabled", True)),
            budget_tokens=int(d.get("budget_tokens", 4096)),
            level=str(d.get("level", "")),
            selectable=bool(d.get("selectable", True)),
        )
    if kind == "disabled":
        return DisabledThinking(enabled=bool(d.get("enabled", False)))
    return None


@dataclass
class AgentConfig:
    """Configuration for a single named agent within a session.

    Agents are multi-turn personas the user can switch between via the
    frontend dropdown.  Each agent has its own system prompt, model
    selection, and tool-permission mode.  The active agent is tracked by
    :attr:`EncreConfig.active_agent_index`.
    """
    name: str = ""
    description: str = ""
    system_prompt: str = ""
    model_index: int = 0
    permission_mode: PermissionMode = "bypass"
    max_turns: int = 0
    enabled_skills: list[str] = field(default_factory=list)
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize this agent config to a plain dictionary.

        Returns:
            A dict with every field set to its current value.
        """
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "model_index": self.model_index,
            "permission_mode": self.permission_mode,
            "max_turns": self.max_turns,
            "enabled_skills": list(self.enabled_skills),
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentConfig:
        """Deserialize an :class:`AgentConfig` from a plain dictionary.

        Args:
            d: Dict produced by :meth:`to_dict` (or a raw config file entry).

        Returns:
            A new :class:`AgentConfig` populated from *d*.
        """
        return cls(
            name=d.get("name", ""),
            description=d.get("description", ""),
            system_prompt=d.get("system_prompt", ""),
            model_index=d.get("model_index", 0),
            permission_mode=d.get("permission_mode", "bypass"),
            max_turns=d.get("max_turns", 0),
            enabled_skills=d.get("enabled_skills", []),
            enabled=d.get("enabled", True),
        )


def _optional_float(value: Any) -> float | None:
    """Parse an optional numeric field, returning ``None`` for unset/invalid.

    Empty strings (unset form inputs), ``None`` and non-finite values all
    map to ``None`` so a cleared input removes the override entirely.
    """
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _optional_int(value: Any) -> int | None:
    """Parse an optional integer field, returning ``None`` for unset/invalid."""
    f = _optional_float(value)
    return None if f is None else int(f)


@dataclass
class ModelConfig:
    """Configuration for a single AI model provider.

    Each entry represents one model endpoint that the user can select from
    the frontend dropdown or API.
    """
    name: str = ""
    model_id: str = ""
    backend_type: str = ""
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 4096
    # context_window: max input tokens; 0 = auto-detect from model/backend,
    # >0 = explicit override that forces the backend to use this cap.
    context_window: int = 0
    enabled: bool = True
    multimodal: bool = False
    # Records whether the provider endpoint was probed as supporting
    # multimodal (image/audio/video) content: "supported", "unsupported"
    # or "unknown". "unknown" means the probe did not run or was
    # inconclusive (e.g. an unexpected API error).
    multimodal_support: str = "unknown"
    # Reasoning levels VERIFIED live against the endpoint during model
    # validation (e.g. ["low", "high", "max"]).  This is a probe result,
    # never a static claim -- providers change level support constantly,
    # so the list only contains levels the endpoint actually accepted.
    # Empty = no level accepted (thinking is on/off only or unsupported).
    reasoning_levels: list[str] = field(default_factory=list)
    # Custom HTTP headers stamped from the owning provider at flatten time
    # (runtime view only; the provider list is the persistence source).
    headers: dict[str, str] = field(default_factory=dict)
    # Verified per-endpoint capability map: canonical node name (see
    # ``encre.backends.multimodal._NODE_*``) -> "supported" / "unsupported".
    # Results are filled in the background after model validation, never
    # blocking the user.  Missing / empty means "not probed yet".
    capabilities: dict[str, str] = field(default_factory=dict)
    thinking_config: ThinkingConfig | None = None
    # User-configured sampling overrides (provider "advanced config").
    # ``None`` means "not set -- use the backend default".  Some relay
    # servers force-require these fields, so an explicit value must be
    # stamped into every request body when present.
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    # Validation stamp: the settings dialog records the identity of the exact
    # probe (credentials + endpoint + model id + toggles) that this model
    # passed.  A saved entry whose stamp matches on the next save is treated
    # as already validated -- only new or changed rows are re-probed, so
    # adding one model to a four-model provider no longer re-runs four probes.
    # Empty = never validated.
    validated_key: str = ""

    def to_dict(self, encrypt_api_keys: bool = True) -> dict[str, Any]:
        """Serialize this model config to a plain dictionary, optionally encrypting the API key.

        Args:
            encrypt_api_keys: When True (default), the ``api_key`` field is
                encrypted via :func:`encre.crypto.encrypt` before inclusion
                in the output so the persisted TOML stays safe.
        """
        encrypted_key = ""
        if self.api_key and encrypt_api_keys:
            try:
                encrypted_key = encrypt(self.api_key)
            except Exception:
                encrypted_key = self.api_key
        else:
            encrypted_key = self.api_key
        result: dict[str, Any] = {
            "name": self.name,
            "model_id": self.model_id,
            "backend_type": self.backend_type,
            "api_key": encrypted_key,
            "base_url": self.base_url,
            "max_tokens": self.max_tokens,
            "context_window": self.context_window,
            "enabled": self.enabled,
            "multimodal": self.multimodal,
            "multimodal_support": self.multimodal_support,
            "reasoning_levels": list(self.reasoning_levels or []),
            "headers": dict(self.headers or {}),
            "capabilities": dict(self.capabilities or {}),
        }
        tc = _thinking_config_to_dict(self.thinking_config)
        if tc is not None:
            result["thinking_config"] = tc
        if self.temperature is not None:
            result["temperature"] = float(self.temperature)
        if self.top_p is not None:
            result["top_p"] = float(self.top_p)
        if self.top_k is not None:
            result["top_k"] = int(self.top_k)
        if self.validated_key:
            result["validated_key"] = str(self.validated_key)
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModelConfig:
        """Deserialize a :class:`ModelConfig` from a plain dictionary.

        Automatically decrypts the stored (possibly-encrypted) ``api_key``
        field.  If decryption fails the raw value is kept so the config
        remains loadable even after a key rotation.
        """
        raw_key = str(d.get("api_key", ""))
        decrypted_key = raw_key
        if raw_key and raw_key != "":
            try:
                decrypted_key = decrypt(raw_key)
            except Exception:
                decrypted_key = raw_key
        return cls(
            name=str(d.get("name", "")),
            model_id=str(d.get("model_id", "")),
            backend_type=str(d.get("backend_type", "")),
            api_key=decrypted_key,
            base_url=str(d.get("base_url", "")),
            max_tokens=int(d.get("max_tokens", 4096)),
            context_window=int(d.get("context_window", 0)),
            enabled=bool(d.get("enabled", True)),
            multimodal=bool(d.get("multimodal", False)),
            multimodal_support=str(d.get("multimodal_support", "unknown")),
            reasoning_levels=[
                str(x) for x in (d.get("reasoning_levels") or [])
                if isinstance(x, str | int | float)
            ],
            headers={
                str(k): str(v) for k, v in (d.get("headers") or {}).items()
            } if isinstance(d.get("headers"), dict) else {},
            capabilities={str(k): str(v) for k, v in (d.get("capabilities") or {}).items()}
            if isinstance(d.get("capabilities"), dict) else {},
            thinking_config=_thinking_config_from_dict(d.get("thinking_config")),
            temperature=_optional_float(d.get("temperature")),
            top_p=_optional_float(d.get("top_p")),
            top_k=_optional_int(d.get("top_k")),
            validated_key=str(d.get("validated_key", "") or ""),
        )


@dataclass
class ProviderConfig:
    """Configuration for one AI service provider.

    A provider owns the connection credentials (``backend_type`` /
    ``api_key`` / ``base_url``) and the list of custom model endpoints
    defined under it.  The provider is the single source of truth for the
    credentials -- the per-model copies are stamped at flatten time by
    :attr:`EncreConfig.models` and never persisted separately.
    """
    backend_type: str = ""
    # User-defined display name.  Only custom providers (openai_compatible)
    # carry one; built-in providers are identified by their catalog label.
    # Every provider name -- built-in label or custom name -- lives in a
    # single namespace, so duplicates are rejected at update time.
    name: str = ""
    api_key: str = ""
    base_url: str = ""
    # Custom HTTP headers applied to every request this provider sends
    # (OpenCode's ``options.headers`` equivalent).  User values override
    # the backend's defaults (Content-Type / Authorization) per-header.
    headers: dict[str, str] = field(default_factory=dict)
    # Provider-level sampling overrides (advanced config).  Applied to every
    # model under this provider at flatten time, mirroring ``headers``.
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    models: list[ModelConfig] = field(default_factory=list)
    # Master switch for the whole provider.  When False every model under it
    # is treated as unavailable (see :attr:`EncreConfig.models`), while the
    # per-model ``enabled`` flags stay untouched so switching the provider
    # back on restores exactly the previous per-model selection.
    enabled: bool = True

    def to_dict(self, encrypt_api_keys: bool = True) -> dict[str, Any]:
        """Serialize this provider, optionally encrypting the API key.

        Model entries are serialized WITHOUT credentials: the provider owns
        ``backend_type`` / ``api_key`` / ``base_url`` for all of its models.
        """
        encrypted_key = ""
        if self.api_key and encrypt_api_keys:
            try:
                encrypted_key = encrypt(self.api_key)
            except Exception:
                encrypted_key = self.api_key
        else:
            encrypted_key = self.api_key
        result: dict[str, Any] = {
            "backend_type": self.backend_type,
            # Custom-only display name; built-in providers keep it empty.
            "name": self.name,
            "api_key": encrypted_key,
            "base_url": self.base_url,
            "headers": dict(self.headers or {}),
            # Provider master switch (green/white toggle in the model table).
            "enabled": bool(self.enabled),
            "models": [
                # Headers are provider-owned: strip the runtime-stamped copy
                # from each model so they are never persisted per model.
                {k: v for k, v in m.to_dict(encrypt_api_keys=False).items() if k != "headers"}
                if isinstance(m, ModelConfig) else m
                for m in self.models
            ],
        }
        # Sampling overrides are provider-owned too: strip the stamped copy
        # from each model and persist the values once at the provider level.
        if self.temperature is not None:
            result["temperature"] = float(self.temperature)
        if self.top_p is not None:
            result["top_p"] = float(self.top_p)
        if self.top_k is not None:
            result["top_k"] = int(self.top_k)
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProviderConfig:
        """Deserialize a :class:`ProviderConfig`, decrypting the API key.

        Every model entry is stamped with the provider credentials so the
        runtime :attr:`EncreConfig.models` view is always complete.
        """
        raw_key = str(d.get("api_key", ""))
        decrypted_key = raw_key
        if raw_key:
            try:
                decrypted_key = decrypt(raw_key)
            except Exception:
                decrypted_key = raw_key
        provider = cls(
            backend_type=str(d.get("backend_type", "")),
            name=str(d.get("name", "")),
            api_key=decrypted_key,
            base_url=str(d.get("base_url", "")),
            headers={
                str(k): str(v) for k, v in (d.get("headers") or {}).items()
            } if isinstance(d.get("headers"), dict) else {},
            temperature=_optional_float(d.get("temperature")),
            top_p=_optional_float(d.get("top_p")),
            top_k=_optional_int(d.get("top_k")),
            enabled=bool(d.get("enabled", True)),
        )
        raw_models = d.get("models") or []
        if isinstance(raw_models, list):
            for m in raw_models:
                mc = m if isinstance(m, ModelConfig) else ModelConfig.from_dict(m)
                mc.backend_type = provider.backend_type
                mc.api_key = provider.api_key
                mc.base_url = provider.base_url
                mc.headers = dict(provider.headers or {})
                # Sampling overrides are PER MODEL now: a model's own value
                # always wins; the legacy provider-level copy (old configs)
                # only fills in when the model has none.
                if mc.temperature is None:
                    mc.temperature = provider.temperature
                if mc.top_p is None:
                    mc.top_p = provider.top_p
                if mc.top_k is None:
                    mc.top_k = provider.top_k
                provider.models.append(mc)
        return provider


def _group_models_into_providers(models: list[ModelConfig]) -> list[ProviderConfig]:
    """Group a flat :class:`ModelConfig` list into providers.

    Legacy configurations (and legacy writers) store one flat list where
    every entry repeats its credentials.  Entries sharing the same
    ``(backend_type, api_key, base_url, headers)`` tuple belong to one
    provider.
    """
    providers: list[ProviderConfig] = []
    by_key: dict[tuple, ProviderConfig] = {}
    for mc in models:
        if not isinstance(mc, ModelConfig):
            continue
        # Sampling is per-model, so it is NOT part of the grouping key:
        # models under one provider may carry different overrides.
        key = (
            mc.backend_type,
            mc.api_key,
            mc.base_url,
            tuple(sorted((mc.headers or {}).items())),
        )
        provider = by_key.get(key)
        if provider is None:
            provider = ProviderConfig(
                backend_type=mc.backend_type,
                api_key=mc.api_key,
                base_url=mc.base_url,
                headers=dict(mc.headers or {}),
            )
            by_key[key] = provider
            providers.append(provider)
        provider.models.append(mc)
    return providers


# The data root now lives in :mod:`encre.paths` so that
# :mod:`encre.config_store` can resolve paths without importing this module
# (which would be an import cycle).  ``get_data_dir`` is re-exported above so
# every existing ``from encre.config import get_data_dir`` keeps working.


def _get_config_path() -> Path:
    """Return the path of the legacy monolithic config file.

    The canonical configuration now lives in
    :mod:`encre.config_store` (``<data_dir>/config/*.json``); this shim is
    retained for the few callers that still reference the old location, and
    deliberately no longer creates the ``model/`` directory.
    """
    return config_store.legacy_config_path()


def _find_config_file(explicit_path: str | None = None) -> Path | None:
    if explicit_path:
        p = Path(explicit_path).expanduser().resolve()
        return p if p.exists() else None
    p = _get_config_path()
    if p.exists():
        return p
    return None


# ---------------------------------------------------------------------------
# Encrypted env-value store (platform credentials)
# ---------------------------------------------------------------------------

#: Name of the encrypted store holding platform env values, relative to the
#: config directory (``<data_dir>/config/env_store.json``).
_ENV_STORE_NAME = "env_store.json"

_env_store_loaded = False


def _env_store_path() -> Path:
    """Return the encrypted env-value store path."""
    return config_store.config_dir() / _ENV_STORE_NAME


def _read_env_store() -> dict[str, str]:
    """Read and decrypt the env-value store; ``{}`` when absent/unusable."""
    try:
        data = config_store._read_encrypted_json(_env_store_path())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def load_env_store() -> None:
    """Decrypt-load persisted env values into ``os.environ`` (idempotent).

    Called from :meth:`EncreConfig.from_file` so credentials saved by the
    platform setup wizards (:func:`save_env_value`) are visible to adapters
    that read ``os.environ`` at runtime.  Already-exported environment
    entries win, so an explicit variable always takes precedence over the
    persisted store.
    """
    global _env_store_loaded
    if _env_store_loaded:
        return
    _env_store_loaded = True
    for name, value in _read_env_store().items():
        os.environ.setdefault(name, value)


def get_env_value(name: str, default: str | None = None) -> str | None:
    """Return *name* from ``os.environ``, loading the env store first."""
    load_env_store()
    return os.environ.get(name, default)


def save_env_value(name: str, value: str) -> None:
    """Persist *name* = *value* (encrypted) and expose it in ``os.environ``.

    The value lands in ``<data_dir>/config/env_store.json`` using the same
    encrypted atomic-write pattern as the other config group files, so
    plaintext credentials never touch the disk.  Raises on write failure
    (fail-closed) -- the caller can surface the error instead of silently
    losing a credential.
    """
    load_env_store()
    value = str(value)
    os.environ[name] = value
    store = _read_env_store()
    store[name] = value
    config_store._write_encrypted_json(_env_store_path(), store)


@dataclass
class SubAgentConfig:
    """Configuration for a named sub-agent that can be invoked by the main agent."""
    name: str = ""
    description: str = ""
    system_prompt: str = ""
    hidden: bool = False
    source: str = "user"
    # ``tool_policy`` constrains the tools a sub-agent may invoke.
    # ``"readonly"`` (Explore-style): read-only tools only.
    # ``"no_writes"`` (Plan-style): no write-class tools, but can
    #    ask questions / run analysis.
    # ``"all"`` (General-purpose): default, full tool access.
    tool_policy: str = "all"
    # ``permission_mode`` is the approval mode applied to the spawned
    # sub-agent ("default" / "auto" / "bypass" / "custom").  An empty
    # value means "inherit the parent agent's mode" — the historical
    # behaviour before this field existed.
    permission_mode: str = ""
    # ``model_index`` pins the execution model to an entry of the parent's
    # provider registry.  ``None`` means "inherit the model the main agent
    # is currently running" (``active_model_index``), so built-in roles
    # follow the parent's model switches automatically.
    model_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "hidden": self.hidden,
            "source": self.source,
            "tool_policy": self.tool_policy,
            "permission_mode": self.permission_mode,
            "model_index": self.model_index,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SubAgentConfig:
        raw_model_index = d.get("model_index")
        return cls(
            name=d.get("name", ""),
            description=d.get("description", ""),
            system_prompt=d.get("system_prompt", ""),
            hidden=d.get("hidden", False),
            source=d.get("source", "user"),
            tool_policy=d.get("tool_policy", "all"),
            permission_mode=str(d.get("permission_mode", "") or ""),
            model_index=int(raw_model_index) if isinstance(raw_model_index, (int, float)) else None,
        )


@dataclass
class PermissionRule:
    """A single structured permission rule.

    ``tool`` matches the tool name (exact, or fnmatch-style wildcard);
    ``pattern`` is matched with fnmatch semantics against the invocation
    text ``"<tool>(<primary_arg>)"`` (e.g. ``Bash(git *)`` matches a Bash
    call whose primary argument starts with ``"git "``); a pattern without
    parentheses additionally matches the primary argument alone.
    ``effect`` is one of ``allow`` / ``ask`` / ``deny``.
    """

    tool: str = "*"
    pattern: str = "*"
    effect: str = "ask"

    def to_dict(self) -> dict[str, Any]:
        """Serialize this rule to a plain dictionary."""
        return {"tool": self.tool, "pattern": self.pattern, "effect": self.effect}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PermissionRule:
        """Deserialize a :class:`PermissionRule` from a plain dictionary."""
        return cls(
            tool=str(d.get("tool", "*") or "*"),
            pattern=str(d.get("pattern", "*") or "*"),
            effect=str(d.get("effect", "ask") or "ask"),
        )


@dataclass
class NetworkPolicyConfig:
    """Outbound network policy for shell commands (``permissions.network``).

    ``mode`` controls how network-capable shell commands (curl, wget,
    ping, nc, language HTTP clients, ...) are treated before execution:

    * ``allow``      -- no restriction (default).
    * ``deny``       -- any network-touching command is refused.
    * ``allowlist``  -- only commands whose target domains match an entry
      in ``allowlist`` are permitted; entries are lowercase domain
      patterns (fnmatch wildcards such as ``*.example.com`` supported).
    """

    mode: str = "allow"
    allowlist: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize this config to a plain dictionary."""
        return {"mode": self.mode, "allowlist": list(self.allowlist)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NetworkPolicyConfig:
        """Deserialize a :class:`NetworkPolicyConfig` from a plain dictionary."""
        raw = d.get("allowlist") or []
        return cls(
            mode=str(d.get("mode", "allow") or "allow"),
            allowlist=[str(x) for x in raw if isinstance(x, str | int)],
        )


@dataclass
class PermissionsConfig:
    """Structured tool permission rules (the ``permissions`` section).

    Rules are scanned in order with exact tool names and exact patterns
    taking priority over wildcards; the first matching rule decides.  Calls
    no rule matches fall back to ``default_effect``.  A missing section
    means ``default_effect="ask"`` with an empty rule list.
    """

    default_effect: str = "ask"
    rules: list[PermissionRule] = field(default_factory=list)
    # Explicit user-authorized paths outside the workspace roots.  The path
    # guard (encre.tools.path_guard) exempts exactly these trees -- and
    # nothing else -- from the workspace containment check.
    extra_paths: list[str] = field(default_factory=list)
    # Outbound network policy for shell commands (see NetworkPolicyConfig).
    network: NetworkPolicyConfig = field(default_factory=NetworkPolicyConfig)

    def to_dict(self) -> dict[str, Any]:
        """Serialize this config to a plain dictionary."""
        return {
            "default_effect": self.default_effect,
            "rules": [
                r.to_dict() if isinstance(r, PermissionRule) else r
                for r in self.rules
            ],
            "extra_paths": list(self.extra_paths),
            "network": (
                self.network.to_dict()
                if isinstance(self.network, NetworkPolicyConfig)
                else self.network
            ),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PermissionsConfig:
        """Deserialize a :class:`PermissionsConfig` from a plain dictionary."""
        raw_rules = d.get("rules") or []
        rules = [
            PermissionRule.from_dict(r) if isinstance(r, dict) else r
            for r in raw_rules
            if isinstance(r, dict | PermissionRule)
        ]
        raw_extra = d.get("extra_paths") or []
        raw_network = d.get("network")
        return cls(
            default_effect=str(d.get("default_effect", "ask") or "ask"),
            rules=rules,
            extra_paths=[str(p) for p in raw_extra if isinstance(p, str)],
            network=(
                NetworkPolicyConfig.from_dict(raw_network)
                if isinstance(raw_network, dict)
                else NetworkPolicyConfig()
            ),
        )


@dataclass
class EncreConfig:
    model: str = ""
    host: str = "localhost"
    port: int = 8000
    api_key: str = ""
    base_url: str = ""
    max_tokens: int = 4096
    max_turns: int = 0
    tool_result_max_chars: int = 80000
    permission_mode: PermissionMode = "bypass"
    slash_command_mode: str = ""
    # Active slash *command* (distinct from a mode).  A command is a
    # sticky one-shot-style prompt injection: once activated it stays in
    # effect across turns (re-injected each run) until explicitly cleared,
    # but it is NOT a mode (no tool interception, no spec gate).  Mirrors
    # ``session.metadata["active_command"]`` so it survives restart.  The
    # dict carries ``name``/``prompt``/``icon``/``title``.  ``None`` = none.
    active_command: dict[str, Any] | None = None
    sandbox_enabled: bool = True
    workspace: str = ""
    session_max_age_hours: float = 24.0
    thinking_config: ThinkingConfig | None = None
    backend_type: str = ""
    backend_kwargs: dict[str, Any] = field(default_factory=dict)
    enable_prompt_caching: bool = True
    enable_streaming_tool_execution: bool = True
    enable_multi_stage_compact: bool = True
    enable_unified_recovery: bool = True
    enable_context_collapse: bool = True
    enable_project_rules: bool = True
    # Context files (AGENTS.md / CLAUDE.md at the workspace root) that are
    # disabled per-workspace.  A disabled file is never loaded into the rules
    # prompt, so the model cannot see its contents.  Filenames only, e.g.
    # ``["CLAUDE.md"]``; the workspace settings UI toggles these per file.
    disabled_context_files: list[str] = field(default_factory=list)
    enable_file_watcher: bool = False
    # Fallback model: auto-switched when the primary model fails (rate limit,
    # overload, etc.).  Empty string = no fallback.  Mirrors Claude Code's
    # fallbackModel in query.ts.
    fallback_model: str = ""
    fallback_base_url: str = ""
    fallback_api_key: str = ""
    fallback_backend_type: str = ""
    # Classifier model: small/fast model for the autosafety classifier.
    # Empty string = auto-select cheapest model per backend type.
    # Mirrors Claude Code's classifierModel setting.
    classifier_model: str = ""
    # Slot reservation: initial output token budget for the first API call
    # each turn.  Escalates to max_tokens when the model hits this limit
    # (finish_reason = "max_tokens" or "length").  0 = disabled (use max_tokens
    # directly).  Mirrors Claude Code's slot reservation in query.ts.
    default_slot_tokens: int = 0
    # Thinking prefill: inject a thinking hint to guide the model's
    # reasoning process.  Mirrors Hermes agent's thinking prefill.
    thinking_prefill_enabled: bool = False
    # Token budget: total output token budget across all turns (0 = unlimited).
    # When exhausted, the model gets one grace call to wrap up.
    # Mirrors Claude Code's token budget (+500k style).
    token_budget: int = 0
    enable_global_rules: bool = True
    checkpoint_max_count: int = 10
    telemetry_enabled: bool = True
    log_level: str = "INFO"
    active_model_index: int = 0
    # Provider-centric model configuration.  Each provider owns the
    # credentials and the custom model endpoints defined under it.  ``models``
    # below is the flattened runtime view over these providers.
    providers: list[ProviderConfig] = field(default_factory=list)
    # Per-run model restriction (e.g. a gateway adapter's selected target
    # models, or an automation job's pinned model).  When non-empty, only these
    # model ids are eligible for a run, falling back to the indicator model and
    # then to a random enabled model on failure.
    target_model_ids: list[str] = field(default_factory=list)
    # Workspace model-pool enforcement state, set by _apply_workspace_config:
    # "off" (no pool -> unrestricted), "on" (pool configured -> restricted to
    # target_model_ids), "empty" (pool enabled but no models selected -> runs
    # must be refused).  Runtime-only; never persisted.
    workspace_pool_state: str = "off"
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    enabled_skills: list[str] = field(default_factory=list)
    # Tool loading tier override: tool names listed here are lifted from the
    # deferred zone into the core (always-visible) set at plugin injection.
    # Use for plugin tools the model needs every turn without declaring
    # ``activation = "core"`` in the plugin manifest.  Empty = default
    # tiering (all plugin tools deferred, loaded on demand via the
    # ``manage`` tool's discover action).
    tool_pin: list[str] = field(default_factory=list)
    system_prompt: str = ""
    default_specialty: str = "general"
    language: str = "zh"
    language_preference: str = "auto"
    default_link_behavior: str = "ask"
    auto_expand: bool = True
    sub_agent_auto_open_view: bool = True
    automation_auto_open_view: bool = False
    # State-machine turn budgets for plan/spec modes (对话流 panel).  The
    # configure handler routes them onto the live config, to_dict +
    # save_config bucket them into the ui overlay store, and goal.py reads
    # them live from there.  -1 = unlimited, 0 = never auto-push, a positive
    # integer = that many pushes at most.
    plan_mode_max_turns: int = -1
    spec_mode_max_turns: int = -1
    shortcut_send_mode: str = "enter"
    default_search_engine: str = "bing"
    default_search_engine_url: str = "https://www.bing.com/search?q={query}"
    startup_session_mode: str = "normal"
    startup_session_behavior: str = "new"
    # Auxiliary-panel display preferences (terminal / editor / review).
    # Pure presentation knobs for the desktop's auxiliary panels; they are
    # written by the settings UI through the same ``configure`` path as the
    # rest of the UI keys and have no effect on the agent loop.  The renderer
    # reads them when a panel is built AND re-applies them live on change, so
    # every key here must have a real consumer in ``session_inner.ts`` -- a
    # key with no consumer is a fake setting and does not belong in this list.
    terminal_font_size: int = 13
    terminal_cursor_blink: bool = True
    terminal_scrollback: int = 10000
    # Font stack id (resolved to a CSS family list in the renderer), and the
    # xterm line height multiplier.
    terminal_font_family: str = "default"
    terminal_line_height: float = 1.3
    # Cursor shape while focused / while the terminal has no focus.
    terminal_cursor_style: str = "bar"
    terminal_cursor_inactive: str = "outline"
    terminal_bold_bright: bool = True
    # Bell handling: xterm 6 has no bellStyle option, so the renderer owns the
    # behaviour ("none" swallows onBell, "visual" flashes the terminal body).
    terminal_bell: str = "visual"
    terminal_copy_on_select: bool = False
    # Right mouse button: context menu, direct paste, or xterm word selection.
    terminal_right_click: str = "menu"
    # "auto" lets the fit addon size the grid; "80"/"120" pin the column count.
    terminal_cols_mode: str = "auto"
    # Shell path chosen in the settings UI ("auto" = first detected shell).
    terminal_shell: str = "auto"
    # New terminals start in the user home or the active workspace root.
    terminal_start_dir: str = "home"
    # Remaining live xterm 6 options.  All are read AT USE TIME by
    # ``_applyTerminalSettings`` and written straight onto ``term.options``.
    terminal_cursor_width: int = 1
    terminal_font_weight: str = "normal"
    terminal_letter_spacing: int = 0
    terminal_min_contrast: int = 1
    terminal_convert_eol: bool = False
    terminal_word_separator: str = "  ()[]{}\",'`"
    editor_font_size: int = 13
    editor_word_wrap: bool = False
    editor_minimap: bool = False
    # Monaco view options.  All of them are live-mutable through
    # ``editor.updateOptions``, so the renderer re-applies them to every open
    # view on change -- no reopen required.
    editor_line_numbers: bool = True
    editor_tab_size: int = 4
    editor_detect_indentation: bool = True
    # "none" / "boundary" / "selection" / "all" -- Monaco's own vocabulary.
    editor_render_whitespace: str = "selection"
    editor_cursor_blink: bool = True
    editor_font_ligatures: bool = False
    editor_sticky_scroll: bool = True
    # Persist the buffer to its file shortly after each edit instead of
    # waiting for the user to press save.  Only applies to tabs that came from
    # a real path; a buffer with no file is never written.
    editor_auto_save: bool = False
    # The rest of the view options worth exposing.  All are plain
    # ``IEditorOptions`` members, so they are live through ``updateOptions``.
    editor_tab_indent: str = "indent"
    editor_use_spaces: bool = True
    editor_word_wrap_column: int = 80
    editor_folding: bool = True
    editor_glyph_margin: bool = False
    editor_format_on_paste: bool = False
    editor_rulers: bool = True
    # "line" | "underneath" | "none" | "boundary" -- Monaco's own vocabulary.
    editor_cursor_style: str = "line"
    # "smooth" | "phase" | "solid" | "expand" | "fill"
    editor_cursor_blinking: str = "smooth"
    review_split_view: bool = False
    review_collapse_unified: bool = False
    # The four knobs the review panel's own action menu already toggles; they
    # are surfaced here so the choice survives across sessions and modes.
    review_wrap: bool = False
    review_word_diff: bool = False
    review_hide_whitespace: bool = False
    review_full_file: bool = True
    # Which change set the review panel opens on: "all" | "unstaged" |
    # "staged" | "branch" | "commit" | "lastRound" -- the panel's own filter
    # vocabulary.  "lastRound" is what a session without a workspace uses.
    review_default_filter: str = "all"
    # Content lines a diff may render before it is summarised instead.  The
    # "show full diff" switch used to be a hardcoded 4000 / 400 pair.
    review_max_lines: int = 4000
    # Start URL of a fresh browser tab.  The sentinel ``"search"`` means
    # "follow the default search engine's homepage"; anything else is taken as
    # a literal URL.  A sentinel rather than "" because the config snapshot
    # omits empty strings, which would make "clear my homepage" un-settable.
    browser_homepage: str = "search"
    # Gate the history write the webview makes on every committed navigation.
    browser_record_history: bool = True
    # Header-level privacy switches; both live in the main process and are
    # pushed there over the ``browser-options`` IPC channel.
    browser_do_not_track: bool = False
    browser_deny_site_permissions: bool = False
    # Refuse a page's window.open / target=_blank instead of opening a tab.
    # Consumed by the webview's window-open handler in the main process.
    browser_block_popups: bool = False
    # Guest page zoom as a percentage; the renderer and the main-process zoom
    # lock both re-assert this number instead of the constant they used to.
    # The panel is narrow, so 75% is the default that actually fits a diff.
    browser_zoom_percent: int = 75
    # With this off the download goes straight into ``browser_download_dir``.
    browser_ask_before_download: bool = True
    # ``"default"`` means the operating system's download folder -- a sentinel
    # rather than "" because the config snapshot omits empty strings.
    browser_download_dir: str = "default"
    # Strip the Cookie header from cross-origin subresources (documents keep
    # theirs, so the site you actually visit still signs you in).
    browser_block_third_party_cookies: bool = False
    # Wipe cookies and site storage once per launch, so nothing stays signed in
    # across sessions.  Bookmarks and history are untouched.
    browser_fresh_session: bool = False
    # Pin guest pages to their light stylesheet.  Off lets a page follow the
    # app theme; the historical behaviour (and default) is pinned light.
    browser_force_light_page: bool = True
    # Browsing identity sent in the UA, client hints and navigator data:
    # desktop Chrome (default) or an Android Chrome phone.
    browser_identity: str = "desktop"
    # Cap on stored history entries; trimming happens on the next visit.
    browser_history_limit: int = 5000
    model_pool_fallback_enabled: bool = False
    model_pool_enabled: bool = True
    # ── Model pool cycling strategy ───────────────────────────────────
    # ONE global policy, inherited automatically by every pool scope
    # (global pool / workspace pool / gateway-adapter pool): a scoped pool
    # restricts WHICH models cycle, and this block governs HOW they cycle.
    # Custom cycle order (drag-configured in the pool settings).  Empty
    # list = the pool's own list order (the default).  A scoped pool
    # filters this order down to its own members; members missing from the
    # list are appended in pool-list order.
    model_pool_cycle_order: list[str] = field(default_factory=list)
    # After a rate limit (429) the model is excluded from cycling for this
    # many milliseconds (cooldown).  -1 (default) = unlimited: the model
    # stays out of the pool until the cycle is reset; 0 = no cooldown (may
    # be retried immediately); N > 0 = N milliseconds.
    model_pool_cooldown_ms: int = -1
    # When every pool model is tried-and-cooling: -1 (default) = keep
    # retrying forever (5s between rounds, bounded by the per-turn recovery
    # budget); 0 = retry immediately; N > 0 = wait N milliseconds before
    # clearing the round and starting from the top.
    model_pool_recycle_wait_ms: int = -1
    # Cap on fallback switches per turn.  -1 / 0 = no extra cap (follow the
    # shared recovery budget); N > 0 = at most N switches.
    model_pool_max_fallbacks: int = -1
    agents: list[AgentConfig] = field(default_factory=list)
    active_agent_index: int = -1
    sub_agents: list[SubAgentConfig] = field(default_factory=list)
    # The tool policy currently active for *this* loop.  Set to the
    # role-template's policy (``"readonly"``, ``"no_writes"``, or
    # ``"all"``) when the loop is spawned as a sub-agent.  The
    # ``_enforce_tool_policy`` hook reads this to decide whether a
    # tool call should be blocked.  Defaults to ``"all"`` for the
    # main agent and for sub-agents that are not policy-restricted.
    current_tool_policy: str = "all"
    dangerous_command_patterns: list[str] = field(default_factory=list)
    adapter_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # OpenTelemetry / OpenInference tracing
    tracing_enabled: bool = False
    tracing_endpoint: str = ""
    tracing_service_name: str = "encre"
    # Optional OTLP/HTTP (JSON) telemetry-log export (the ``telemetry.otlp``
    # section).  Zero overhead when disabled: no exporter or thread is built.
    telemetry_otlp_enabled: bool = False
    telemetry_otlp_endpoint: str = ""
    telemetry_otlp_service_name: str = "encre-harness"
    telemetry_otlp_export_interval_seconds: int = 30

    # Permission override settings (like OS permissions)
    # Maps: tool_or_feature_name → "allow" | "deny" | "ask"
    # Examples:
    #   "bash" → "ask"       — ask every time bash runs
    #   "file_write" → "allow"  — always allow file writes
    #   "network" → "deny"      — block all network access
    #   "sandbox" → "allow"     — allow sandbox isolation
    # Empty = use mode-based default for that tool.
    permission_settings: dict[str, str] = field(default_factory=dict)

    # Structured permission rules (the ``permissions`` section).  Evaluated
    # before the mode-aware engine check; the first matching rule wins
    # (exact beats wildcard).  A missing section defaults to
    # ``default_effect="ask"`` with no rules.
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)

    # Device context acquisition.  When enabled, the agent collects a
    # lightweight device catalog (OS, host, timezone, available device_*
    # tools) at startup and injects it into the system prompt so the model
    # knows what device information is available on demand.  Detailed data
    # is fetched lazily via the device tool with actions: info, location,
    # sensor, battery, network (display details come from
    # the computer tool's display_info action).
    device_context_enabled: bool = True
    # Cache TTL (seconds) for collected device context before re-probing.
    device_context_cache_ttl: int = 86400
    # Per-provider opt-in whitelist (see device/context/manager.py): a
    # non-empty list narrows the catalog to exactly those providers, an
    # empty list means "no restriction", i.e. every provider stays on.
    # Provider names: platform_info, hardware_info, gpu_info, display_info,
    # network_info, location_info, sensor_info, battery_info.
    device_context_providers: list[str] = field(default_factory=list)
    # Network proxy (settings panel -> General -> Network).  Routing knobs
    # consumed by ``encre.proxy_env.apply_network_proxy``: the mode is
    # mirrored onto the standard proxy environment variables at startup and
    # on every configure patch, so every HTTP stack in the process (httpx /
    # OpenAI backends, gateway adapters, requests-based tools) follows it
    # without per-client plumbing.  mode: system | manual | direct.
    network_proxy_mode: str = "system"
    network_proxy_scheme: str = "http"
    network_proxy_host: str = ""
    network_proxy_port: int = 0
    network_proxy_username: str = ""
    network_proxy_password: str = ""
    # Comma-separated hosts that bypass the proxy (mirrored onto NO_PROXY).
    network_proxy_bypass: str = ""

    @property
    def models(self) -> list[ModelConfig]:
        """Flattened runtime view over :attr:`providers`.

        Returns every provider's models in declaration order with the
        provider credentials and headers stamped onto each entry, so
        consumers (backends, failover, gateway) keep seeing complete
        :class:`ModelConfig` objects.  The provider list is the single
        source of truth; assigning a flat list to this property re-groups
        it into providers by ``(backend_type, api_key, base_url, headers)``.
        """
        out: list[ModelConfig] = []
        for provider in self.providers:
            provider_on = bool(getattr(provider, "enabled", True))
            for mc in provider.models:
                # A provider that is switched off reports every one of its
                # models as disabled, but the canonical per-model flags are
                # left untouched -- switching the provider back on restores
                # exactly the selection the user had before.
                target = replace(mc, enabled=False) if not provider_on else mc
                target.backend_type = provider.backend_type
                target.api_key = provider.api_key
                target.base_url = provider.base_url
                target.headers = dict(provider.headers or {})
                out.append(target)
        return out

    @models.setter
    def models(self, value: list[ModelConfig] | None) -> None:
        raw = value or []
        self.providers = _group_models_into_providers([
            mc if isinstance(mc, ModelConfig) else ModelConfig.from_dict(mc)
            for mc in raw
        ])

    def get_enabled_models(self) -> list[ModelConfig]:
        """Return all enabled models ordered by priority (indicator first).

        The active (indicator) model at ``active_model_index`` is always
        returned first, followed by the remaining enabled models in their
        declared order.  Disabled models are skipped entirely.
        """
        """All enabled models in priority order (indicator first)."""
        if not self.models:
            return []
        ordered = []
        if 0 <= self.active_model_index < len(self.models):
            ordered.append(self.models[self.active_model_index])
        for i, m in enumerate(self.models):
            if i != self.active_model_index:
                ordered.append(m)
        return [m for m in ordered if getattr(m, "enabled", True)]

    def get_model_by_id(self, model_id: str) -> ModelConfig | None:
        for m in self.models:
            if m.model_id == model_id:
                return m
        return None

    def resolve_model_candidates(
        self,
        tried: set[str] | None = None,
        cooldowns: dict[str, float] | None = None,
    ) -> list[ModelConfig]:
        """Return an ordered list of candidate models for a new API call.

        Ordering: with a custom cycle order (``model_pool_cycle_order``,
        drag-configured in the pool settings) the pool members follow that
        order exactly — members missing from it are appended in pool-list
        order, and a scoped pool (target_model_ids) filters it down to its
        own members.  Without one: target-model IDs first (pinned by
        adapter/automation), then the indicator model, then the rest
        shuffled so repeated failures do not deterministically loop over
        the same model.  Models whose IDs appear in *tried* are filtered
        out, as are models whose cooldown (``model_id -> cooldown-end
        timestamp``) has not expired.
        """
        tried = tried or set()
        order = [str(x) for x in (getattr(self, "model_pool_cycle_order", None) or []) if str(x)]
        if order:
            # Custom (drag-configured) cycle order wins.  Entries are
            # provider-qualified ("provider<US>model_id") so the SAME model_id
            # declared under different providers cycles as SEPARATE pool
            # members; legacy bare-id entries (written before qualification)
            # match the first provider that declares them.  DISABLED ones
            # keep their slot (they are filtered out below, so re-enabling
            # restores the exact position), and any configured member missing
            # from the list is appended in pool-list order.  A scoped pool
            # (target_model_ids) simply filters this down to its own members
            # -- the strategy is one global policy inherited by every scope.
            avail: dict[str, ModelConfig] = {}
            first_key_by_model_id: dict[str, str] = {}
            for provider in self.providers:
                pkey = (
                    provider.name
                    if provider.backend_type == "openai_compatible"
                    else provider.backend_type
                )
                for mc in provider.models:
                    av_key = f"{pkey}\u001f{mc.model_id}"
                    avail.setdefault(av_key, mc)
                    first_key_by_model_id.setdefault(mc.model_id, av_key)
            ordered = []
            seen = set()
            for key in order:
                # Qualified entries resolve directly; anything else is
                # treated as a legacy bare model_id.
                av_key = key if key in avail else first_key_by_model_id.get(key)
                if av_key is None or av_key in seen:
                    continue
                ordered.append(avail[av_key])
                seen.add(av_key)
            for av_key, m in avail.items():
                if av_key not in seen:
                    ordered.append(m)
                    seen.add(av_key)
        else:
            enabled = self.get_enabled_models()
            if not enabled:
                return []
            ordered = []
            seen = set()

            # 1. Explicit target models (per-adapter / per-job restriction) —
            #    pool membership, so they keep priority.
            for tid in getattr(self, "target_model_ids", None) or []:
                m = self.get_model_by_id(tid)
                if m and getattr(m, "enabled", True) and m.model_id not in seen:
                    ordered.append(m)
                    seen.add(m.model_id)

            # 2. The indicator (active) model -- get_enabled_models() returns
            #    it first.
            if enabled and enabled[0].model_id not in seen:
                ordered.append(enabled[0])
                seen.add(enabled[0].model_id)

            # 3. Remaining enabled models, shuffled so repeats are
            #    unpredictable.
            rest = [m for m in enabled if m.model_id not in seen]
            random.shuffle(rest)
            ordered.extend(rest)

        remaining = [m for m in ordered if m.model_id not in tried] if tried else ordered
        # A scoped pool (adapter / automation pin) is a hard membership
        # filter: candidates never leave the pinned set, custom order or not.
        _targets = getattr(self, "target_model_ids", None) or []
        if _targets:
            _tset = {str(t) for t in _targets}
            remaining = [m for m in remaining if m.model_id in _tset]
        # The cycle never lands on a disabled model (they only reserve their
        # slot in the custom order).
        remaining = [m for m in remaining if getattr(m, "enabled", True)]
        if cooldowns:
            # Rate-limited models stay out of the pool until their cooldown
            # expires (timestamp in the future => still cooling).
            now = time.time()
            remaining = [m for m in remaining if cooldowns.get(m.model_id, 0) <= now]
        return remaining

    def get_active_model(self) -> ModelConfig:
        """Return the currently-active (indicator) model, or a minimal fallback.

        Prefers ``self.models[self.active_model_index]`` when the index
        is valid and the model is enabled; otherwise falls back to the
        first enabled model.  When no models exist a synthetic minimal
        :class:`ModelConfig` is returned from the top-level flat fields.
        """
        if self.models:
            # Prefer the currently selected index if it is enabled; otherwise fall
            # back to the first enabled model in the list. Disabled models are
            # ignored by agent runs and gateway adapters.
            candidates = []
            if 0 <= self.active_model_index < len(self.models):
                candidates.append(self.models[self.active_model_index])
            for i, m in enumerate(self.models):
                if i != self.active_model_index:
                    candidates.append(m)
            for m in candidates:
                if getattr(m, "enabled", True):
                    return m
        return ModelConfig(
            name=self.model,
            model_id=self.model,
            backend_type=self.backend_type,
            api_key=self.api_key,
            base_url=self.base_url,
            max_tokens=self.max_tokens,
        )

    def apply_active_model(self) -> None:
        """Pull the active model's fields into the flat config properties.

        After this call ``self.model``, ``self.backend_type``,
        ``self.api_key``, ``self.base_url`` and ``self.max_tokens`` all
        reflect the indicator model instead of any stale top-level value.
        The model's ``context_window`` is passed through
        ``backend_kwargs`` when non-zero so the backend honours it.
        """
        active = self.get_active_model()
        if active.name:
            self.model = active.model_id
            self.backend_type = active.backend_type
            self.api_key = active.api_key
            self.base_url = active.base_url
            self.max_tokens = active.max_tokens
            # Pass context_window through backend_kwargs so the backend
            # can honour it when computing context_window_size().
            if active.context_window > 0:
                self.backend_kwargs["context_window"] = active.context_window
            else:
                self.backend_kwargs.pop("context_window", None)

    # Sampling keys a model config can carry (provider "advanced config").
    _SAMPLING_KEYS = ("temperature", "top_p", "top_k")

    def active_model_sampling_overrides(self) -> dict[str, float | int]:
        """Return the active model's configured sampling parameters.

        Every ``create_backend`` call site must spread this so the user's
        temperature / top_p / top_k reach ``backend.sampling_overrides``
        and the outgoing request body -- relay servers that force-require
        these fields reject the loop's bare defaults.  Unset values are
        omitted, leaving the backend's internal defaults in charge.
        """
        active = self.get_active_model()
        out: dict[str, float | int] = {}
        if getattr(active, "temperature", None) is not None:
            out["temperature"] = float(active.temperature)
        if getattr(active, "top_p", None) is not None:
            out["top_p"] = float(active.top_p)
        if getattr(active, "top_k", None) is not None:
            out["top_k"] = int(active.top_k)
        return out

    def get_active_agent(self) -> AgentConfig | None:
        """Return the currently-active agent, or ``None`` when unset."""
        if self.agents and 0 <= self.active_agent_index < len(self.agents):
            return self.agents[self.active_agent_index]
        return None

    def get_permission_rules(self) -> tuple[list[PermissionRule], str]:
        """Return the permission rules and default effect (cached).

        The compiled snapshot is cached on the instance and invalidated by
        the identity of the :attr:`permissions` object: replacing the object
        (config reload / settings update) refreshes the cache, while the
        common per-tool-call path skips re-normalising the rule list.

        Returns:
            ``(rules, default_effect)`` where *rules* contains only
            :class:`PermissionRule` entries in configuration order and
            *default_effect* is one of ``allow`` / ``ask`` / ``deny``.
        """
        cached = getattr(self, "_permission_rules_cache", None)
        if cached is not None and cached[0] is self.permissions:
            return cached[1]
        perms = self.permissions
        if not isinstance(perms, PermissionsConfig):
            perms = PermissionsConfig()
        effect = perms.default_effect
        if effect not in ("allow", "ask", "deny"):
            effect = "ask"
        rules = [r for r in perms.rules if isinstance(r, PermissionRule)]
        snapshot = (rules, effect)
        self._permission_rules_cache = (perms, snapshot)
        return snapshot

    def get_network_policy(self) -> tuple[str, list[str]]:
        """Return the shell network policy as ``(mode, allowlist)`` (cached).

        Cached like :meth:`get_permission_rules`: the snapshot is keyed on
        the identity of the :attr:`permissions` object, so replacing the
        object (config reload / settings update) refreshes the cache.
        *mode* is one of ``allow`` / ``deny`` / ``allowlist`` (invalid
        values fall back to ``allow``); *allowlist* entries are trimmed,
        lowercased domain patterns.
        """
        cached = getattr(self, "_network_policy_cache", None)
        if cached is not None and cached[0] is self.permissions:
            return cached[1]
        perms = self.permissions
        if not isinstance(perms, PermissionsConfig):
            perms = PermissionsConfig()
        net = (
            perms.network
            if isinstance(perms.network, NetworkPolicyConfig)
            else NetworkPolicyConfig()
        )
        mode = net.mode if net.mode in ("allow", "deny", "allowlist") else "allow"
        allowlist = [str(d).strip().lower() for d in net.allowlist if str(d).strip()]
        snapshot = (mode, allowlist)
        self._network_policy_cache = (perms, snapshot)
        return snapshot

    @classmethod
    def from_file(cls, path: str | None = None) -> EncreConfig:
        """Load an :class:`EncreConfig`.

        With no *path* the split layout under ``<data_dir>/config/`` is read
        (splitting the legacy ``model/config.toml`` on first run); an explicit
        *path* is still read as a single self-contained encrypted file, which
        keeps the ``--config`` CLI flag working for one-off runs.

        Every recognised key is mapped onto the dataclass fields.  Adapter
        configs stored as flat ``adapter_<id>_<key>`` keys are reassembled
        into ``self.adapter_configs``; similarly ``backend_kwargs.<key>``
        keys are collected under ``self.backend_kwargs``.
        """
        config_dict: dict[str, Any] = {}

        # Restore persisted platform credentials into os.environ before any
        # consumer reads the environment (see load_env_store / get_env_value).
        load_env_store()

        if path is None:
            config_dict = config_store.load_config()
        else:
            found = _find_config_file(path)
            if found is not None and found.exists():
                raw_text = found.read_text(encoding="utf-8").strip()
                if raw_text:
                    try:
                        import json as _json
                        config_dict = _json.loads(decrypt(raw_text))
                    except Exception:
                        # Corrupted or stale encrypted config (e.g. key changed) -
                        # start fresh instead of crashing the whole service.
                        config_dict = {}

        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs: dict[str, Any] = {}
        adapter_cfgs: dict[str, dict[str, Any]] = {}
        # Legacy layouts persist a flat ModelConfig list under "models";
        # the provider list is canonical now.  Prefer "providers" when both
        # are present and group the flat list otherwise.
        raw_models = config_dict.pop("models", None)
        for key, value in config_dict.items():
            if key in valid_keys:
                kwargs[key] = value
            elif key.startswith("adapter_"):
                parts = key.split("_", 2)
                if len(parts) >= 3:
                    adapter_id = parts[1]
                    field_key = parts[2]
                    adapter_cfgs.setdefault(adapter_id, {})[field_key] = value
            elif key.startswith("backend_kwargs."):
                bk_key = key.split(".", 1)[1]
                kwargs.setdefault("backend_kwargs", {})[bk_key] = value
        if adapter_cfgs:
            kwargs["adapter_configs"] = adapter_cfgs

        if kwargs.get("api_key"):
            with contextlib.suppress(Exception):
                kwargs["api_key"] = decrypt(str(kwargs["api_key"]))
            # keep as-is if decryption fails (legacy plaintext)

        if "providers" in kwargs and isinstance(kwargs["providers"], list):
            kwargs["providers"] = [
                ProviderConfig.from_dict(p) if isinstance(p, dict) else p
                for p in kwargs["providers"]
            ]
        elif isinstance(raw_models, list) and raw_models:
            kwargs["providers"] = _group_models_into_providers([
                ModelConfig.from_dict(m) if isinstance(m, dict) else m
                for m in raw_models
            ])

        if "agents" in kwargs and isinstance(kwargs["agents"], list):
            kwargs["agents"] = [
                AgentConfig.from_dict(a) if isinstance(a, dict) else a
                for a in kwargs["agents"]
            ]

        if "sub_agents" in kwargs and isinstance(kwargs["sub_agents"], list):
            kwargs["sub_agents"] = [
                SubAgentConfig.from_dict(s) if isinstance(s, dict) else s
                for s in kwargs["sub_agents"]
            ]

        if "permissions" in kwargs and isinstance(kwargs["permissions"], dict):
            kwargs["permissions"] = PermissionsConfig.from_dict(kwargs["permissions"])

        cfg = cls(**kwargs)
        if cfg.models:
            cfg.apply_active_model()
        return cfg

    @classmethod
    def from_env(cls) -> EncreConfig:
        return cls.from_file(path=None)

    _MODEL_FLAT_FIELDS = frozenset({"model", "api_key", "base_url", "max_tokens", "backend_type"})
    _SKIP_TO_DICT = frozenset({"adapter_configs", "providers", "agents", "sub_agents", "workspace_pool_state"})

    def to_dict(self, encrypt_api_keys: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {}
        has_models = bool(self.models)
        for field_info in self.__dataclass_fields__.values():
            if field_info.name in self._SKIP_TO_DICT or (has_models and field_info.name in self._MODEL_FLAT_FIELDS):
                continue
            elif field_info.name == "api_key":
                raw = self.api_key
                if raw and encrypt_api_keys:
                    try:
                        result["api_key"] = encrypt(raw)
                    except Exception:
                        result["api_key"] = raw
                else:
                    result["api_key"] = raw or ""
            elif field_info.name == "permissions":
                result["permissions"] = (
                    self.permissions.to_dict()
                    if isinstance(self.permissions, PermissionsConfig)
                    else self.permissions
                )
            else:
                result[field_info.name] = getattr(self, field_info.name)

        # Providers (canonical) + flattened models (derived view for
        # frontend snapshots and single-file exports).
        if self.providers:
            result["providers"] = [
                p.to_dict(encrypt_api_keys=encrypt_api_keys) if isinstance(p, ProviderConfig) else p
                for p in self.providers
            ]
        flat_models = self.models
        if flat_models:
            result["models"] = [m.to_dict(encrypt_api_keys=encrypt_api_keys) if isinstance(m, ModelConfig) else m for m in flat_models]
        if self.agents:
            result["agents"] = [a.to_dict() if isinstance(a, AgentConfig) else a for a in self.agents]
        if self.sub_agents:
            result["sub_agents"] = [s.to_dict() if isinstance(s, SubAgentConfig) else s for s in self.sub_agents]

        # Flatten adapter_configs -> flat adapter_* keys for frontend compatibility
        for adapter_id, fields in self.adapter_configs.items():
            for field_key, field_value in fields.items():
                result[f"adapter_{adapter_id}_{field_key}"] = field_value

        return result

    def save(self, path: str | None = None) -> None:
        """Persist *self* to disk.

        With no *path* (or when *path* is the legacy ``model/config.toml``,
        which is what the ``configure`` handler passes) the configuration is
        split by responsibility across ``<data_dir>/config/*.json`` and each
        file is encrypted and replaced atomically -- so "the model config
        file" no longer receives agents, adapters and UI preferences.

        An explicit *path* that is **not** the legacy path keeps the old
        single-file behaviour, so ``save(custom_path)`` still produces one
        self-contained encrypted config (used by tests and tooling).
        """
        data = {k: v for k, v in self.to_dict().items() if v is not None}
        if path is None or config_store.is_legacy_config_path(path):
            # "models" is the flattened provider view -- derived at read
            # time, never persisted (the provider list is canonical).
            data.pop("models", None)
            config_store.save_config(data)
            return
        import json
        raw = json.dumps(data, ensure_ascii=False, indent=2)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(encrypt(raw), encoding="utf-8")
