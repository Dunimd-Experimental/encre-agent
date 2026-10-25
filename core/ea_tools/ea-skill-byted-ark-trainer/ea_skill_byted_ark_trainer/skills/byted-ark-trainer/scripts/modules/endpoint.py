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
Endpoint management module.

Provides CRUD operations for Volcengine ARK inference endpoints. Each
endpoint serves a custom fine-tuned model over HTTPS. The module wraps
the ``volcenginesdkark`` SDK and handles credential loading from
environment variables.

Functions are named to match the CLI subcommands defined in
``ark_trainer_helper.py`` under the ``endpoint`` module (create, list,
get, certificate, stop, delete).
"""

import os
from dotenv import load_dotenv
from volcenginesdkark.api.ark_api import ARKApi
from volcenginesdkark.models.create_endpoint_request import CreateEndpointRequest
from volcenginesdkark.models.model_reference_for_create_endpoint_input import (
    ModelReferenceForCreateEndpointInput,
)
from volcenginesdkark.models.list_endpoints_request import ListEndpointsRequest
from volcenginesdkark.models.get_endpoint_request import GetEndpointRequest
from volcenginesdkark.models.get_endpoint_certificate_request import (
    GetEndpointCertificateRequest,
)
from volcenginesdkark.models.stop_endpoint_request import StopEndpointRequest
from volcenginesdkark.models.delete_endpoint_request import DeleteEndpointRequest
from volcenginesdkcore.configuration import Configuration


def init_api_client():
    """
    Initialize and return a configured ARK API client instance.

    Loads credentials from the ``VOLCENGINE_ACCESS_KEY`` and
    ``VOLCENGINE_SECRET_KEY`` environment variables (or from a ``.env``
    file via ``python-dotenv``). Configures the SDK to target the
    ``cn-beijing`` region and sets it as the process-wide default.

    Returns:
        An ``ARKApi`` instance ready for endpoint operations, or
        ``None`` if credentials are missing.
    """
    load_dotenv()
    access_key = os.getenv("VOLCENGINE_ACCESS_KEY")
    secret_key = os.getenv("VOLCENGINE_SECRET_KEY")

    if not access_key or not secret_key:
        print("Error: Authentication credentials are required")
        print("Set the environment variables VOLCENGINE_ACCESS_KEY and VOLCENGINE_SECRET_KEY")
        return None

    # Configure the SDK client with credentials and target region
    config = Configuration()
    config.ak = access_key
    config.sk = secret_key
    config.region = "cn-beijing"
    config.endpoint = "ark.cn-beijing.volces.com"

    # Register as the process-wide default so SDK methods that do not
    # accept an explicit config will use these settings.
    Configuration.set_default(config)

    # Use the parameterless constructor; it reads from the default config.
    return ARKApi()


def create_endpoint(api, args):
    """
    Create a new inference endpoint for a custom model.

    Uses the ``--custom-model-id`` argument to reference the model to
    deploy. If no custom model ID is provided, the function exits early
    with an error message.

    Args:
        api: An initialized ``ARKApi`` client instance.
        args: Parsed CLI arguments containing ``name``, ``description``,
              ``custom_model_id``, and ``project_name``.

    Returns:
        The endpoint ID string on success, or ``None`` on failure.
    """
    print("=== Create Endpoint ===")

    # Build the model reference for the endpoint creation request
    model_reference = ModelReferenceForCreateEndpointInput()

    # Only custom model IDs are supported; foundation models require a
    # different code path not covered by this helper.
    if args.custom_model_id:
        model_reference.custom_model_id = args.custom_model_id
        print(f"Using custom model: {args.custom_model_id}")
    else:
        print("Error: --custom-model-id argument is required")
        return None

    # Construct the CreateEndpoint request object with all user-specified fields
    request = CreateEndpointRequest(
        name=args.name,
        description=args.description,
        model_reference=model_reference,
        project_name=args.project_name,
    )

    try:
        response = api.create_endpoint(request)
        print(f"Created successfully! Endpoint ID: {response.id}")
        return response.id
    except Exception as e:
        print(f"Creation failed: {str(e)}")
        return None


def list_endpoints(api, args):
    """
    List all inference endpoints with optional pagination.

    Retrieves endpoints from the specified project and prints a summary
    table including ID, name, and status for each endpoint.

    Args:
        api: An initialized ``ARKApi`` client instance.
        args: Parsed CLI arguments containing ``page_size``,
              ``page_number``, and ``project_name``.

    Returns:
        A list of endpoint objects on success, or an empty list on failure.
    """
    print("\n=== List Endpoints ===")

    # Build the ListEndpoints request with pagination parameters
    request = ListEndpointsRequest(
        page_size=args.page_size,
        page_number=args.page_number,
        project_name=args.project_name,
    )

    try:
        response = api.list_endpoints(request)
        print(f"Found {len(response.items)} endpoints")
        for item in response.items:
            print(f"- ID: {item.id}, Name: {item.name}, Status: {item.status}")
        return response.items
    except Exception as e:
        print(f"List failed: {str(e)}")
        return []


def get_endpoint(api, args):
    """
    Retrieve detailed information about a single endpoint.

    Fetches and displays endpoint metadata including name, status,
    description, creation time, and the referenced custom model ID.

    Args:
        api: An initialized ``ARKApi`` client instance.
        args: Parsed CLI arguments containing ``endpoint_id``.

    Returns:
        The endpoint response object on success, or ``None`` on failure.
    """
    print(f"\n=== Get Endpoint Details ({args.endpoint_id}) ===")

    # Build the GetEndpoint request using the endpoint ID from CLI args
    request = GetEndpointRequest(id=args.endpoint_id)

    try:
        response = api.get_endpoint(request)
        print(f"Endpoint name: {response.name}")
        print(f"Endpoint status: {response.status}")
        print(f"Endpoint description: {response.description}")
        print(f"Created at: {response.create_time}")
        # The model_reference field may contain either a custom_model_id
        # or a foundation_model; we only display custom_model_id here.
        if response.model_reference:
            if (
                hasattr(response.model_reference, "custom_model_id")
                and response.model_reference.custom_model_id
            ):
                print(f"Custom model ID: {response.model_reference.custom_model_id}")
        return response
    except Exception as e:
        print(f"Fetch failed: {str(e)}")
        return None


def get_endpoint_certificate(api, args):
    """
    Retrieve the TLS certificate details for an endpoint.

    Fetches and displays the certificate's validity period and PCA
    (Private Certificate Authority) host information, which is needed
    for setting up secure connections to the endpoint.

    Args:
        api: An initialized ``ARKApi`` client instance.
        args: Parsed CLI arguments containing ``endpoint_id``.

    Returns:
        The certificate response object on success, or ``None`` on failure.
    """
    print(f"\n=== Get Endpoint Certificate ({args.endpoint_id}) ===")

    # Build the GetEndpointCertificate request
    request = GetEndpointCertificateRequest(id=args.endpoint_id)

    try:
        response = api.get_endpoint_certificate(request)
        print(f"Certificate valid from: {response.not_before}")
        print(f"Certificate valid until: {response.not_after}")
        print(f"PCA name: {response.pca_name}")
        print(f"PCA host: {response.pca_host}")
        return response
    except Exception as e:
        print(f"Fetch failed: {str(e)}")
        return None


def stop_endpoint(api, args):
    """
    Stop a running inference endpoint.

    Sends a stop request to gracefully shut down the endpoint. The
    endpoint becomes unavailable for new requests after the stop is
    processed, but can be restarted later.

    Args:
        api: An initialized ``ARKApi`` client instance.
        args: Parsed CLI arguments containing ``endpoint_id``.

    Returns:
        The stop response object on success, or ``None`` on failure.
    """
    print(f"\n=== Stop Endpoint ({args.endpoint_id}) ===")

    # Build the StopEndpoint request
    request = StopEndpointRequest(id=args.endpoint_id)

    try:
        response = api.stop_endpoint(request)
        print("Stop request submitted successfully")
        return response
    except Exception as e:
        print(f"Stop failed: {str(e)}")
        return None


def delete_endpoint(api, args):
    """
    Permanently delete an inference endpoint.

    Sends a delete request to remove the endpoint entirely. This action
    is irreversible; the endpoint cannot be recovered after deletion.

    Args:
        api: An initialized ``ARKApi`` client instance.
        args: Parsed CLI arguments containing ``endpoint_id``.

    Returns:
        The delete response object on success, or ``None`` on failure.
    """
    print(f"\n=== Delete Endpoint ({args.endpoint_id}) ===")

    # Build the DeleteEndpoint request
    request = DeleteEndpointRequest(id=args.endpoint_id)

    try:
        response = api.delete_endpoint(request)
        print("Delete request submitted successfully")
        return response
    except Exception as e:
        print(f"Delete failed: {str(e)}")
        return None
