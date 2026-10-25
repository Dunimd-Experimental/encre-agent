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

"""Shared CLI helpers and configuration plumbing for the ACEP API tool.

Every command module in this package imports ``cli_common`` as ``cli``
and uses a small set of utilities to:

* resolve credentials and optional TOS settings from environment
  variables or a ``config.json`` file (``get_client`` / ``load_config``),
* inject common request parameters such as ``ProductId`` into API
  calls (``request_kwargs``),
* format API responses as indented JSON for stdout (``print_result``),
* parse user-supplied CLI values into typed Python objects
  (``parse_csv_values``, ``parse_bool_flag``, ``parse_json_option``,
  ``parse_key_value_params``, ``parse_app_list``, etc.).

The module keeps a single ``_config_path_override`` global so that
tests can inject an alternate config path without touching the
environment.  The override is set by ``set_config_path`` and takes
precedence over the default ``config.json`` lookup.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .vephone_client import VePhoneClient

_config_path_override: Optional[str] = None


def set_config_path(config_path: Optional[str]) -> None:
    """Override the config-file search path for subsequent ``load_config`` calls.

    Primarily used by test suites to point at a fixture file without
    mutating the real environment.  Passing ``None`` clears the
    override and restores default behaviour.
    """
    global _config_path_override
    _config_path_override = config_path


def _config_candidates(config_path: str) -> list[Path]:
    """Build the ordered list of directories to probe for the config file.

    An absolute path is returned as a single-element list.  A relative
    path is first looked up in the current working directory, then
    walked up the package-tree starting from this module's own
    location, so the CLI finds ``config.json`` whether it is invoked
    from the repo root, the ``scripts/`` directory, or any other
    ancestor.
    """
    candidate = Path(config_path).expanduser()
    if candidate.is_absolute():
        return [candidate]

    candidates = [Path.cwd() / candidate]
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidates.append(parent / candidate)
    return candidates


def load_config(config_path: Optional[str] = None) -> dict:
    """Load and parse the JSON configuration file.

    Resolution order: explicit ``config_path`` argument >
    ``_config_path_override`` global > ``config.json``.  The first
    existing file in the candidate list wins; if none exist an empty
    dict is returned so callers can fall back to environment variables
    or defaults without an extra conditional.

    Args:
        config_path: Optional explicit path.  ``None`` triggers the
            override / default lookup described above.

    Returns:
        Parsed JSON object (typically a ``dict``).  Empty dict if no
        config file was found.
    """
    config_path = config_path or _config_path_override or "config.json"
    for candidate in _config_candidates(config_path):
        if candidate.exists():
            with candidate.open("r", encoding="utf-8") as f:
                return json.load(f)
    return {}


def config_value(
    config: dict, key: str, env_key: str, default: Optional[str] = None
) -> Optional[str]:
    """Look up a configuration value by canonical key, env-style key, or default.

    This two-lookup helper lets config.json use either ``access_key``
    (canonical) or ``VOLC_ACCESS_KEY`` (env-style) without duplicating
    parsing logic at every call site.
    """
    return config.get(key) or config.get(env_key) or default


def request_kwargs(args) -> dict:
    """Build a kwargs dict with ``ProductId`` if the parser captured one.

    Every command that needs the product context calls this helper
    instead of constructing the dict inline, which keeps the spread
    operator ``**cli.request_kwargs(args)`` a single, uniform pattern
    across all command modules.
    """
    kwargs = {}
    product_id = getattr(args, "product_id", None)
    if product_id is not None:
        kwargs["ProductId"] = product_id
    return kwargs


def get_client() -> VePhoneClient:
    """Create an authenticated ``VePhoneClient`` from env vars or config.

    Credentials resolution follows a strict priority chain:

    1. Environment variables (``VOLC_ACCESS_KEY``, ``VOLC_SECRET_KEY``,
       ``VOLC_REGION``, optional TOS settings).
    2. If any credential env var is missing, ``load_config()`` is
       invoked and the missing values are back-filled from the config
       file.  ``config_value`` checks both the canonical key and the
       env-style key so either spelling in config.json works.

    If credentials are still unavailable after both passes the CLI
    exits with a message directing the user to set env vars or create
    a config file.
    """
    default_region = "cn-north-1"
    access_key = os.getenv("VOLC_ACCESS_KEY")
    secret_key = os.getenv("VOLC_SECRET_KEY")
    region = os.getenv("VOLC_REGION") or default_region
    tos_bucket = os.getenv("VOLC_TOS_BUCKET")
    tos_region = os.getenv("VOLC_TOS_REGION")
    tos_endpoint = os.getenv("VOLC_TOS_ENDPOINT")
    tos_prefix = os.getenv("VOLC_TOS_PREFIX")

    if not access_key or not secret_key:
        config = load_config()
        access_key = config_value(config, "access_key", "VOLC_ACCESS_KEY")
        secret_key = config_value(config, "secret_key", "VOLC_SECRET_KEY")
        region = os.getenv("VOLC_REGION") or config_value(config, "region", "VOLC_REGION", default_region)
        tos_bucket = config_value(config, "tos_bucket", "VOLC_TOS_BUCKET")
        tos_region = config_value(config, "tos_region", "VOLC_TOS_REGION")
        tos_endpoint = config_value(config, "tos_endpoint", "VOLC_TOS_ENDPOINT")
        tos_prefix = config_value(config, "tos_prefix", "VOLC_TOS_PREFIX")

    if not access_key or not secret_key:
        print(
            "错误: 请设置环境变量 VOLC_ACCESS_KEY 和 VOLC_SECRET_KEY，或配置 config.json 文件"
        )
        sys.exit(1)

    return VePhoneClient(
        access_key,
        secret_key,
        region,
        tos_bucket=tos_bucket,
        tos_region=tos_region,
        tos_endpoint=tos_endpoint,
        tos_prefix=tos_prefix,
    )


def print_result(result: dict, indent: int = 2):
    """Serialize an API response as pretty-printed JSON and write it to stdout.

    ``ensure_ascii=False`` is critical because the API returns Chinese
    characters (app names, descriptions) that must remain human-readable
    in the terminal rather than being escaped as ``\\uXXXX`` sequences.
    """
    print(json.dumps(result, indent=indent, ensure_ascii=False))


def parse_string_params(items: Optional[list], option_name: str = "--payload") -> dict:
    """Parse ``Key=value`` items into a string-keyed dict.

    Used for ad-hoc payload parameters where both key and value are
    opaque strings (no JSON interpretation).  Raises ``SystemExit`` on
    malformed input so the user sees a clear CLI error rather than an
    API 400.
    """
    params = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"{option_name} 必须是 Key=value 格式")
        key, value = item.split("=", 1)
        if not key:
            raise SystemExit(f"{option_name} key 不能为空")
        params[key] = value
    return params


def parse_csv_values(value: Optional[str], value_type=str):
    """Split a comma-separated string into a list of typed items.

    Empty strings and trailing commas produce empty entries that are
    silently dropped.  ``None`` input returns ``None`` (not an empty
    list) so callers can distinguish "not provided" from "provided
    but empty".
    """
    if not value:
        return None
    return [value_type(item) for item in value.split(",") if item != ""]


def parse_csv(value: str) -> list:
    """Simple comma-splitter that filters empty tokens."""
    return [item for item in value.split(",") if item]


def parse_bool_flag(value: str) -> bool:
    """Parse a CLI boolean flag accepting multiple true/false spellings.

    If ``value`` is already a ``bool`` (as argparse can produce for
    ``store_true`` / ``store_false`` actions), it is returned as-is.
    Otherwise the string is normalised via ``lower()`` and matched
    against common truthy/falsy tokens.  Unrecognised values raise
    ``ArgumentTypeError`` which argparse surfaces as a usage error.
    """
    if isinstance(value, bool):
        return value
    lowered = value.lower()
    if lowered in {"true", "1", "yes", "y"}:
        return True
    if lowered in {"false", "0", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("布尔值必须是 true/false")


def parse_json_option(
    value: Optional[str], option_name: str, expected_type: Optional[type] = None
) -> Any:
    """Parse a JSON string and optionally validate its top-level type.

    Raises ``SystemExit`` (not ``JSONDecodeError``) so argparse
    displays the error cleanly without a traceback.  When
    ``expected_type`` is given, the parsed object must be an instance
    of that type, which catches cases like passing a JSON object where
    a list was expected.
    """
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{option_name} 必须是合法 JSON: {exc.msg}") from exc
    if expected_type is not None and not isinstance(parsed, expected_type):
        raise SystemExit(f"{option_name} 必须是 {expected_type.__name__} JSON")
    return parsed


def parse_json_like_value(value: str) -> Any:
    """Best-effort parse of a value that *may* be JSON.

    If the stripped value is non-empty and valid JSON, the decoded
    object is returned (e.g. numbers, booleans, lists, dicts).
    Otherwise the raw string is returned unchanged, which lets callers
    pass plain text keys without quoting.
    """
    stripped = value.strip()
    if stripped == "":
        return ""
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def parse_key_value_params(
    items: Optional[list[str]], option_name: str = "--param"
) -> dict[str, Any]:
    """Parse ``Key=Value`` items where each value may be JSON-typed.

    Unlike ``parse_string_params``, the value portion goes through
    ``parse_json_like_value`` so ``--param count=3 --param enabled=true``
    yields ``{'count': 3, 'enabled': True}`` instead of strings.  This
    is the preferred parser for request bodies that mix scalar and
    structured fields.
    """
    params = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"{option_name} 必须是 Key=Value 格式")
        key, value = item.split("=", 1)
        if not key:
            raise SystemExit(f"{option_name} key 不能为空")
        params[key] = parse_json_like_value(value)
    return params


def parse_app_list(value: str) -> list[dict]:
    """Parse an app list from either a JSON array or ``AppId:VersionId`` CSV.

    Two formats are accepted:

    * JSON array of objects: ``[{"AppId": "a1", "VersionId": "v1"}, ...]``
    * Comma-separated CSV: ``a1:v1,a2:v2``

    The JSON path is checked first (by leading ``[``) and validated so
    every element contains both ``AppId`` and ``VersionId``.  The CSV
    path splits on ``:`` and rejects empty halves.  Both paths raise
    ``SystemExit`` with a descriptive message on malformed input.
    """
    stripped = value.strip()
    if stripped.startswith("["):
        parsed = parse_json_option(value, "--app-list", list)
        for item in parsed:
            if (
                not isinstance(item, dict)
                or "AppId" not in item
                or "VersionId" not in item
            ):
                raise SystemExit(
                    "--app-list JSON 数组中的每项都必须包含 AppId 和 VersionId"
                )
        return parsed

    app_list = []
    for item in value.split(","):
        if not item:
            continue
        if ":" not in item:
            raise SystemExit(
                "--app-list 必须是 AppId:VersionId,AppId2:VersionId2 或 JSON 数组"
            )
        app_id, version_id = item.split(":", 1)
        if not app_id or not version_id:
            raise SystemExit("--app-list 中的 AppId 和 VersionId 不能为空")
        app_list.append({"AppId": app_id, "VersionId": version_id})
    if not app_list:
        raise SystemExit("--app-list 不能为空")
    return app_list
