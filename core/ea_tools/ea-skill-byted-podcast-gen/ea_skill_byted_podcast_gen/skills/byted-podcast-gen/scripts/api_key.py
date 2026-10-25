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

"""Resolve a speech-API credential for the byted podcast TTS flow.

The skill can be given a dedicated speech key (``MODEL_SPEECH_API_KEY``)
directly, or be handed only an Ark gateway credential (``ARK_SKILL_API_KEY``
plus ``ARK_SKILL_API_BASE``). In the latter case this module talks to the
Ark "service API key" management endpoints to list — and, when none exists,
create — a speech-scoped key so downstream TTS/WebSocket calls always have
a usable credential without user interaction.

Resolution is cached two ways: in the process environment and, when the
optional ``python-dotenv`` extra is installed, in the skill-local ``.env``
file so repeated invocations skip the network round-trip entirely.
"""

import json
import os
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Ark's control-plane API version this client is pinned to; kept as the
# default so callers can be upgraded by changing one place.
_DEFAULT_API_VERSION = "2025-05-20"


def _build_action_url(base: str, action: str, version: str = _DEFAULT_API_VERSION) -> str:
    """Build an Ark RPC URL of the form ``{base}/?Action=...&Version=...``.

    Ark addresses its operations with query parameters rather than path
    segments, so the incoming ``base`` is parsed, normalised to a
    trailing-slash path (so appending stays unambiguous), and the existing
    query string is decoded, merged with the requested ``Action`` and
    ``Version``, then re-encoded. Returning "" for an empty base lets
    callers treat a missing endpoint as "not configured" without extra
    branching.
    """
    base = (base or "").strip()
    if not base:
        return ""
    parsed = urlparse(base)
    path = parsed.path or "/"
    if not path.endswith("/"):
        path = f"{path}/"
    query = dict(parse_qsl(parsed.query))
    query["Action"] = action
    query["Version"] = version
    return urlunparse(parsed._replace(path=path, query=urlencode(query)))


def _extract_json(text: str) -> dict | None:
    """Parse a JSON object out of a response body, tolerating noise.

    Servers sometimes wrap payloads in log lines or HTML fragments, so the
    fast path is a straight ``json.loads``; when that fails, the function
    falls back to slicing from the first ``{`` to the last ``}`` and
    retrying. Returning ``None`` (instead of raising) keeps callers able to
    distinguish "unparseable" from "parsed but empty".
    """
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None


def _post_json(
    url: str, headers: dict, payload: dict, timeout_s: int = 15
) -> dict | None:
    """POST a JSON payload and return the parsed response body.

    Every failure mode collapses to ``None``: an empty URL short-circuits,
    header values that are ``None`` are skipped (so callers can build
    headers conditionally), and both HTTP errors and transport errors are
    swallowed after attempting to read a structured error body from the
    HTTPError. This makes the function safe to call in a best-effort
    fallback chain.
    """
    if not url:
        return None
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in (headers or {}).items():
        if v is None:
            continue
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return _extract_json(body)
    except urllib.error.HTTPError as e:
        # HTTPError still carries a useful body (error codes/messages);
        # reading it preserves diagnostics without surfacing the exception.
        try:
            body = e.read().decode("utf-8", errors="replace")
            return _extract_json(body)
        except Exception:
            return None
    except Exception:
        return None


def _dotenv_path() -> str:
    """Return the absolute path of the skill-local ``.env`` file.

    Anchoring on ``__file__`` rather than the current working directory
    means the cached credential resolves to the same place regardless of
    where the skill script is invoked from.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_dotenv_if_available(dotenv_path: str) -> None:
    """Load the local ``.env`` into the environment, silently skipping when unavailable.

    ``python-dotenv`` is an optional dependency, so the import is deferred
    into the function body and guarded: a missing package (or any other
    failure) means resolution simply relies on process environment variables
    instead of failing the whole credential lookup.
    """
    from dotenv import load_dotenv  # type: ignore

    try:
        load_dotenv(dotenv_path=dotenv_path, override=False)
    except Exception:
        return


def _persist_env_to_dotenv_if_available(dotenv_path: str, key: str, value: str) -> None:
    """Persist a resolved credential to the local ``.env`` for reuse.

    Persistence is best-effort: a blank value is dropped, the directory is
    created if needed, and the file is chmod'd to ``0o600`` so the secret
    is owner-only where the OS honours file modes. Any failure is ignored
    because the caller has already written the value into ``os.environ``
    for the current run.
    """
    value = (value or "").strip()
    if not value:
        return
    from dotenv import set_key  # type: ignore

    try:
        os.makedirs(os.path.dirname(dotenv_path) or ".", exist_ok=True)
        set_key(dotenv_path, key, value, quote_mode="never")
        try:
            os.chmod(dotenv_path, 0o600)
        except Exception:
            pass
    except Exception:
        return


def get_speech_api_key(
    project_name: str = "default", create_name: str = "arkclaw"
) -> str:
    """Return a usable speech API key, creating one if necessary.

    Resolution order:

    1. Reuse ``MODEL_SPEECH_API_KEY`` (process env, after loading ``.env``) —
       avoids all network traffic on warm starts.
    2. If only an Ark gateway credential is configured, list available
       speech keys for ``project_name`` and adopt the first one.
    3. If the listing yields nothing, create a new key named ``create_name``
       and adopt it.

    Each discovered key is written back to ``os.environ`` and the local
    ``.env`` so subsequent calls take step 1. An empty string signals that
    no credential could be obtained, which the caller converts into a
    descriptive error.
    """
    _load_dotenv_if_available(_dotenv_path())

    speech_key = os.getenv("MODEL_SPEECH_API_KEY", "").strip()
    if speech_key:
        return speech_key

    ark_key = os.getenv("ARK_SKILL_API_KEY", "").strip()
    base = os.getenv("ARK_SKILL_API_BASE", "").strip()
    if not ark_key or not base:
        return ""

    # "ServiceName" scopes the request to the speech product's key pool.
    headers = {
        "ServiceName": "speech_saas_prod",
        "Authorization": f"Bearer {ark_key}",
        "Content-Type": "application/json",
    }

    list_url = _build_action_url(base, "ListAPIKeys")
    list_data = _post_json(
        list_url,
        headers=headers,
        payload={"ProjectName": project_name, "OnlyAvailable": True},
    )
    if isinstance(list_data, dict):
        api_keys = (list_data.get("Result") or {}).get("APIKeys") or []
        if isinstance(api_keys, list) and api_keys:
            first = api_keys[0] if isinstance(api_keys[0], dict) else {}
            first_key = (first.get("APIKey") or "").strip()
            if first_key:
                os.environ["MODEL_SPEECH_API_KEY"] = first_key
                _persist_env_to_dotenv_if_available(
                    _dotenv_path(), "MODEL_SPEECH_API_KEY", first_key
                )
                return first_key

    # Fall back to provisioning a fresh key when the account has none yet.
    create_url = _build_action_url(base, "CreateAPIKey")
    create_data = _post_json(
        create_url,
        headers=headers,
        payload={"ProjectName": project_name, "Name": create_name},
    )
    if isinstance(create_data, dict):
        created_key = ((create_data.get("Result") or {}).get("APIKey") or "").strip()
        if created_key:
            os.environ["MODEL_SPEECH_API_KEY"] = created_key
            _persist_env_to_dotenv_if_available(
                _dotenv_path(), "MODEL_SPEECH_API_KEY", created_key
            )
            return created_key

    return ""
