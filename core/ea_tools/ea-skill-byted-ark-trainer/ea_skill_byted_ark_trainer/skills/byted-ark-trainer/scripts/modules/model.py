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

"""Foundation-model listing for the ARK trainer CLI.

Two listings are exposed:

* :func:`list_foundation_models` ? paged listing of foundation models,
  filtered by name and supported customization type. This talks to the
  VolcEngine ``ark`` service directly via signed HTTP requests.
* :func:`list_foundation_model_versions` ? paged listing of the
  published versions of a single foundation model. This goes through
  the ARK SDK because it is a read-only endpoint that has already been
  modelled by the SDK.

Why the split? The ``list_foundation_models`` endpoint does not have a
stable model on the SDK side and returns a schema that varies by release,
so we sign requests ourselves using the helpers below. ``list_foundation_models``
does not use :func:`_ark_api_request`; it uses the ARK SDK directly
with a small wrapper.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import traceback
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from ark_sdk.core.client.ark import default_ark_client
from ark_sdk.types.foundation_model.foundation_model import (
    ListFoundationModelVersionsRequest,
)

# Load ``.env`` at import time so credentials are visible to every call
# site, not just the ones that remember to call ``load_dotenv()``.
load_dotenv()


# =========================================================================
# ARK API ? signed request plumbing
# =========================================================================


def _ark_api_request(action: str, method: str = "POST", body=None, params=None) -> dict:
    """Issue a signed VolcEngine ``ark`` API request.

    VolcEngine's HMAC-SHA256 signing scheme signs the canonical request
    string built from the HTTP method, path, query parameters, selected
    headers, and the SHA-256 of the body. Getting any of the header
    names wrong (case, order, missing colon) causes signature mismatch
    and a 403 back from the gateway, so the ordering here is deliberate
    and matches the reference implementation.

    Args:
        action: API action name (e.g. ``ListFoundationModels``).
        method: HTTP method; defaults to ``POST``.
        body: Optional JSON-serializable request body.
        params: Optional URL query parameters.

    Returns:
        Parsed JSON response body.

    Raises:
        ValueError: If required credentials are not present in the
            environment.
        requests.HTTPError: On non-2xx responses.
    """
    # Read credentials from the environment. ARK_SESSION_TOKEN is an
    # optional short-lived token used when operating via STS-style auth.
    ak = os.getenv("VOLCENGINE_ACCESS_KEY")
    sk = os.getenv("VOLCENGINE_SECRET_KEY")
    region = os.getenv("ARK_REGION") or os.getenv("REGION") or "cn-beijing"
    session_token = os.getenv("ARK_SESSION_TOKEN") or os.getenv("SESSION_TOKEN")

    if not ak or not sk:
        raise ValueError("??? VOLCENGINE_ACCESS_KEY ? VOLCENGINE_SECRET_KEY ????")

    service = "ark"
    version = "2024-01-01"
    host = f"ark.{region}.volcengineapi.com"
    content_type = "application/json; charset=utf-8"

    # VolcEngine signs against the raw request body bytes, so we
    # serialize once and reuse that exact string for both signing and
    # the outgoing request. Re-serializing would produce a different
    # hash whenever key ordering differs.
    request_body = json.dumps(body) if body else ""

    # The gateway also expects Action + Version in the query string.
    query_params = params or {}
    query_params.update(
        {
            "Action": action,
            "Version": version,
        }
    )

    now = _utc_now()
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_x_date = x_date[:8]

    x_content_sha256 = _hash_sha256(request_body)
    sign_result = {
        "Host": host,
        "X-Content-Sha256": x_content_sha256,
        "X-Date": x_date,
        "Content-Type": content_type,
    }

    # Optional: session-token authentication (rare, only for STS use).
    if session_token:
        sign_result["X-Security-Token"] = session_token

    # Every signed header must be lowercased and sorted.
    signed_headers_list = ["content-type", "host", "x-content-sha256", "x-date"]
    if "X-Security-Token" in sign_result:
        signed_headers_list.append("x-security-token")

    signed_headers_str = ";".join(sorted(signed_headers_list))

    # The canonical request uses lowercase header keys but the actual
    # Authorization header needs the original casing for values.
    header_key_map = {
        "content-type": "Content-Type",
        "host": "Host",
        "x-content-sha256": "X-Content-Sha256",
        "x-date": "X-Date",
        "x-security-token": os.getenv("X-Security-Token", ""),
    }
    header_lines = []
    for header_key in sorted(signed_headers_list):
        header_value = sign_result[header_key_map[header_key]]
        header_lines.append(f"{header_key}:{header_value}")

    canonical_headers_str = "\n".join(header_lines)

    canonical_request_str = "\n".join(
        [
            method.upper(),
            "/",
            _norm_query(query_params),
            canonical_headers_str,
            "",
            signed_headers_str,
            x_content_sha256,
        ]
    )

    hashed_canonical_request = _hash_sha256(canonical_request_str)
    credential_scope = "/".join([short_x_date, region, service, "request"])
    string_to_sign = "\n".join(
        ["HMAC-SHA256", x_date, credential_scope, hashed_canonical_request]
    )

    # Derive the signing key via the standard HMAC-SHA256 chain:
    # date -> region -> service -> "request".
    k_date = _hmac_sha256(sk.encode("utf-8"), short_x_date)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    k_signing = _hmac_sha256(k_service, "request")
    signature = _hmac_sha256(k_signing, string_to_sign).hex()

    sign_result["Authorization"] = (
        "HMAC-SHA256 Credential={}, SignedHeaders={}, Signature={}".format(
            ak + "/" + credential_scope,
            signed_headers_str,
            signature,
        )
    )

    response = requests.request(
        method=method,
        url=f"https://{host}/",
        headers=sign_result,
        params=query_params,
        data=request_body,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _norm_query(params: dict) -> str:
    """Build a canonical query string in the VolcEngine-required form.

    Rules that differ from the default ``urlencode`` output:
    * keys sorted ascending,
    * values quoted with ``safe="-_.~"`` (VolcEngine's stricter set),
    * trailing ``&`` removed,
    * literal ``+`` re-encoded to ``%20`` because the gateway treats
      ``+`` as a space character.
    """
    query = ""
    for key in sorted(params.keys()):
        if isinstance(params[key], list):
            for k in params[key]:
                query = (
                    query + quote(key, safe="-_.~") + "=" + quote(k, safe="-_.~") + "&"
                )
        else:
            query = (
                query
                + quote(key, safe="-_.~")
                + "="
                + quote(params[key], safe="-_.~")
                + "&"
            )
    query = query[:-1]
    return query.replace("+", "%20")


def _hmac_sha256(key: bytes, content: str) -> bytes:
    return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def _hash_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _utc_now() -> datetime.datetime:
    """Return a timezone-aware UTC timestamp.

    Kept as a tiny helper so the caller can rely on the return value
    being timezone-aware even in environments where ``datetime.timezone``
    is not yet available.
    """
    try:
        from datetime import timezone

        return datetime.datetime.now(timezone.utc)
    except ImportError:

        class UTC(datetime.tzinfo):
            def utcoffset(self, dt):
                return datetime.timedelta(0)

            def tzname(self, dt):
                return "UTC"

            def dst(self, dt):
                return datetime.timedelta(0)

        return datetime.datetime.now(UTC())


def list_foundation_models(args):
    """List foundation models matching the caller's filters.

    The ``FoundationModelTag.Domains = ["LLM"]`` filter is hard-coded
    because non-LLM foundation models (embeddings, multimodal) are
    listed through a different endpoint and mixing them here would
    confuse downstream callers.

    Args:
        args: ``argparse.Namespace`` with ``name``,
            ``supported_customization_type``, ``page_size`` and
            ``page_number``.

    Returns:
        The list of item dictionaries returned by the API, or ``None``
        on error.
    """
    print("\n=== Query foundation model list ===")
    try:
        # All list-foundation-model queries are scoped to LLM models.
        filter_params = {"FoundationModelTag": {"Domains": ["LLM"]}}

        # Optional name fuzzy filter.
        if args.name:
            filter_params["Name"] = args.name

        # Optional customization-type filter.
        if args.supported_customization_type:
            filter_params["SupportedCustomizationTypes"] = [
                args.supported_customization_type
            ]

        request_body = {
            "Filter": filter_params,
            "PageNumber": args.page_number,
            "PageSize": args.page_size,
            "SortBy": "CreateTime",
            "SortOrder": "Desc",
        }

        resp_data = _ark_api_request(
            action="ListFoundationModels", method="POST", body=request_body
        )

        result = resp_data.get("Result", {})
        items = result.get("Items", [])
        total_count = result.get("TotalCount", len(items))
        print(f"Found {total_count} foundation models (this page: {len(items)}):")
        print("-" * 150)
        print(f"{'Model Name':<30} {'Version':<15} {'Description'}")
        print("-" * 150)

        for item in items:
            model_name = item.get("Name", "unknown")
            primary_version = item.get("PrimaryVersion", "unknown")
            description = item.get("Description", item.get("DisplayDescription", ""))

            # Truncate long descriptions so the table stays readable.
            if len(description) > 200:
                description = description[:197] + "..."

            print(f"{model_name:<30} {primary_version:<15} {description}")

        return items
    except requests.exceptions.RequestException as e:
        print(f"Query failed: HTTP request error - {str(e)}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Response body: {e.response.text}")
        return None
    except Exception as e:
        print(f"Query failed: {str(e)}")
        traceback.print_exc()
        return None


def list_foundation_model_versions(args):
    """List all published versions of a foundation model.

    Args:
        args: ``argparse.Namespace`` with ``model_name``, ``page_size``
            and ``page_number``.

    Returns:
        The list of version items from the SDK response, or ``None``
        on error.
    """
    print(f"\n=== Query foundation model versions ({args.model_name}) ===")
    try:
        # ARK's canonical model-name format uses dashes; some callers
        # pass dotted names, so normalize here rather than relying on
        # the SDK to be lenient.
        model_name = args.model_name.replace(".", "-")

        req = ListFoundationModelVersionsRequest(
            FoundationModelName=model_name,
            PageSize=args.page_size,
            PageNumber=args.page_number,
        )
        ark_client = default_ark_client()
        resp = ark_client.list_foundation_model_versions(req)

        print(f"Foundation model: {args.model_name}")
        # Tolerate both pydantic v1 (`.dict()`) and v2 (`.model_dump()`)
        # item shapes when serializing the entries below.
        items = getattr(resp, "Items", getattr(resp, "items", []))
        total = len(items)
        print(f"Total versions: {total}")
        print("-" * 100)
        print(f"{'Version':<25} {'Status':<10} {'Created':<25} {'Description'}")
        print("-" * 100)

        for item in items:
            item_dict = (
                item.model_dump()
                if hasattr(item, "model_dump")
                else item.dict()
                if hasattr(item, "dict")
                else dict(item)
            )
            # Fields may be exposed in either CamelCase (raw API) or
            # snake_case (SDK model) depending on how the SDK wrapped
            # them, so we try both spellings.
            model_version = item_dict.get(
                "ModelVersion", item_dict.get("model_version", "unknown")
            )
            status = item_dict.get("Status", item_dict.get("status", "unknown"))
            create_time = item_dict.get(
                "CreateTime", item_dict.get("create_time", "unknown")
            )
            description = item_dict.get("Description", item_dict.get("description", ""))
            print(
                f"{model_version:<25} {status:<10} {create_time:<25} {description or ''}"
            )

        return resp.Items
    except Exception as e:
        print(f"Query failed: {str(e)}")
        return None
