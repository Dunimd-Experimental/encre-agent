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

"""Module: ea_tool_cloud_storage.tool

Cloud object-storage tool for AWS S3, Google Cloud Storage (GCS), and
Azure Blob Storage behind a single tool interface.

Provider SDKs (``boto3``, ``google.cloud.storage``, ``azure.storage.blob``)
are imported lazily inside each action handler so a minimal install that
never touches a specific cloud still works. Credentials are resolved in
this order:

1. Explicit kwargs (``access_key`` / ``secret_key`` / ``credentials_path`` /
   ``connection_string``) -- useful for ephemeral CI sessions.
2. Standard env vars (``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``,
   ``AWS_REGION``, ``GOOGLE_APPLICATION_CREDENTIALS``,
   ``AZURE_STORAGE_CONNECTION_STRING``) -- the recommended path.

Supported actions:

- ``list``      -- list objects in a bucket (all providers).
- ``upload``    -- upload a local file (all providers).
- ``download``  -- download an object to a local path (all providers).
- ``delete``    -- delete a single object (all providers).
- ``info``      -- return object metadata as JSON (all providers).
- ``list_buckets`` -- list all buckets/containers (S3 / GCS only).
- ``sync_up``   -- recursively upload a local directory (S3 only).
"""

from __future__ import annotations

import json
import os
from typing import Any

from encre.tools.base import build_tool


async def _cloud_storage_execute(**kwargs: Any) -> str:
    """Dispatch to the requested provider handler.

    Args:
        kwargs: Tool call arguments. Common keys are ``provider``,
            ``action``, ``bucket``, ``key``, ``local_path``, and ``prefix``;
            provider-specific credentials may be provided inline or read
            from the environment.

    Returns:
        A provider-specific status string on success, or an actionable
        error string if the provider, action, or bucket is invalid.

    Each provider handler is defined in its own module-level coroutine so
    boto3 / google-cloud / azure dependencies only load on demand. The
    ``bucket`` field is required for every action -- the tool cannot infer
    a target from context, so we fail fast here rather than inside each
    provider's own validation.
    """
    provider = kwargs.get("provider", "s3")
    action = kwargs.get("action", "")
    bucket = kwargs.get("bucket", "")
    key = kwargs.get("key", "")
    local_path = kwargs.get("local_path", "")
    prefix = kwargs.get("prefix", "")

    if not bucket:
        return "Error: 'bucket' is required."

    if provider == "s3":
        return await _s3_action(action, bucket, key, local_path, prefix, kwargs)
    elif provider == "gcs":
        return await _gcs_action(action, bucket, key, local_path, prefix, kwargs)
    elif provider == "azure":
        return await _azure_action(action, bucket, key, local_path, prefix, kwargs)
    else:
        return f"Error: Unknown provider '{provider}'. Options: s3, gcs, azure."


async def _s3_action(action: str, bucket: str, key: str, local_path: str, prefix: str, kwargs: dict[str, Any]) -> str:
    """Handle an S3 action against the AWS S3 client.

    Args:
        action: One of ``list`` / ``upload`` / ``download`` / ``delete`` /
            ``info`` / ``list_buckets`` / ``sync_up``.
        bucket: Target bucket name.
        key: Object key (path within the bucket). Required for
            download/delete/info; optional for upload where the local file
            basename is used.
        local_path: Local file (upload) or destination (download), or a
            directory for ``sync_up``.
        prefix: Object key prefix used to scope ``list`` results.
        kwargs: Additional overrides -- ``region``, ``access_key``,
            ``secret_key``, ``max_keys``, ``public``.

    Credentials are pulled from explicit kwargs first and then from the
    standard ``AWS_*`` environment variables, so users running this in CI
    without a ``~/.aws/credentials`` file can still authenticate by
    exporting env vars.
    """
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError

    aws_access_key = kwargs.get("access_key") or os.environ.get("AWS_ACCESS_KEY_ID", "")
    aws_secret_key = kwargs.get("secret_key") or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    region = kwargs.get("region") or os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

    session_args: dict[str, Any] = {"region_name": region}
    if aws_access_key and aws_secret_key:
        session_args["aws_access_key_id"] = aws_access_key
        session_args["aws_secret_access_key"] = aws_secret_key

    try:
        session = boto3.Session(**session_args)
        s3 = session.client("s3")
    except NoCredentialsError:
        return "Error: AWS credentials not found. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY."
    except Exception as e:
        return f"Error initializing S3 client: {e}"

    try:
        if action == "list":
            params: dict[str, Any] = {"Bucket": bucket}
            if prefix:
                params["Prefix"] = prefix
            # Cap the caller's max_keys at 1000 which is the S3 server
            # maximum -- exceeding it would return an opaque server error.
            max_keys = min(kwargs.get("max_keys", 100), 1000)
            params["MaxKeys"] = max_keys
            resp = s3.list_objects_v2(**params)
            contents = resp.get("Contents", [])
            if not contents:
                return f"No objects found in s3://{bucket}/{prefix}"
            total = len(contents)
            out = []
            for obj in contents:
                # Human-friendly size formatting so the listing is
                # scannable without a follow-up `info` call.
                size = obj["Size"]
                if size < 1024:
                    size_str = f"{size}B"
                elif size < 1024 * 1024:
                    size_str = f"{size / 1024:.1f}KB"
                else:
                    size_str = f"{size / 1024 / 1024:.1f}MB"
                out.append(f"{obj['Key']:60s} {size_str:>8s}  {obj['LastModified'].strftime('%Y-%m-%d %H:%M')}")
            return f"s3://{bucket}/ (showing {total} objects):\n" + "\n".join(out)

        elif action == "upload":
            if not local_path:
                return "Error: 'local_path' is required for upload."
            if not os.path.isfile(local_path):
                return f"Error: Local file not found: {local_path}"
            extra_args = {}
            if kwargs.get("public"):
                extra_args["ACL"] = "public-read"
            s3.upload_file(local_path, bucket, key or os.path.basename(local_path), ExtraArgs=extra_args or None)
            dest_key = key or os.path.basename(local_path)
            return f"File uploaded to s3://{bucket}/{dest_key}"

        elif action == "download":
            if not key:
                return "Error: 'key' is required for download."
            dest = local_path or os.path.basename(key)
            os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
            s3.download_file(bucket, key, dest)
            return f"File downloaded: s3://{bucket}/{key} -> {os.path.abspath(dest)}"

        elif action == "delete":
            if not key:
                return "Error: 'key' is required for delete."
            s3.delete_object(Bucket=bucket, Key=key)
            return f"Deleted s3://{bucket}/{key}"

        elif action == "info":
            if not key:
                return "Error: 'key' is required for info."
            resp = s3.head_object(Bucket=bucket, Key=key)
            return json.dumps({
                "key": key,
                "size": resp.get("ContentLength", 0),
                "content_type": resp.get("ContentType", ""),
                "last_modified": str(resp.get("LastModified", "")),
                "etag": resp.get("ETag", ""),
                "metadata": resp.get("Metadata", {}),
            }, indent=2)

        elif action == "list_buckets":
            resp = s3.list_buckets()
            buckets = resp.get("Buckets", [])
            out = [f"{b['Name']:40s} {b['CreationDate'].strftime('%Y-%m-%d')}" for b in buckets]
            return "S3 Buckets:\n" + "\n".join(out) if out else "No buckets found."

        elif action == "sync_up":
            if not local_path:
                return "Error: 'local_path' is required for sync_up."
            if not os.path.isdir(local_path):
                return f"Error: Directory not found: {local_path}"
            uploaded = 0
            for root, _dirs, files in os.walk(local_path):
                for fname in files:
                    full_path = os.path.join(root, fname)
                    # Preserve the relative directory structure so the
                    # remote layout matches the local one, and normalise
                    # path separators to "/" so S3 keys are portable.
                    rel_path = os.path.relpath(full_path, local_path)
                    s3_key = f"{prefix}{rel_path}" if prefix else rel_path
                    s3_key = s3_key.replace("\\", "/")
                    s3.upload_file(full_path, bucket, s3_key)
                    uploaded += 1
            return f"Synced {uploaded} files to s3://{bucket}/"

        else:
            return f"Error: Unknown S3 action '{action}'. Options: list, upload, download, delete, info, list_buckets, sync_up."

    except ClientError as e:
        return f"AWS error: {e.response['Error']['Message']}"
    except Exception as e:
        return f"S3 error: {e}"


async def _gcs_action(action: str, bucket: str, key: str, local_path: str, prefix: str, kwargs: dict[str, Any]) -> str:
    """Handle an action against Google Cloud Storage.

    Args:
        action: One of ``list`` / ``upload`` / ``download`` / ``delete`` /
            ``info`` / ``list_buckets``.
        bucket: Target bucket name.
        key: Object name (path within the bucket). Required for
            download/delete/info; optional for upload.
        local_path: Local file (upload) or destination (download).
        prefix: Object name prefix used to scope ``list`` results.
        kwargs: Additional overrides -- ``credentials_path``, ``max_keys``,
            ``public``.

    The client is constructed from an explicit service-account JSON path
    (``credentials_path`` or ``GOOGLE_APPLICATION_CREDENTIALS``) or by
    falling back to the ambient default credentials (useful on App Engine
    / Cloud Run / developer machines with ``gcloud auth login`` already
    done).
    """
    from google.cloud import storage
    from google.cloud.exceptions import NotFound

    try:
        credentials_path = kwargs.get("credentials_path") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        client = storage.Client.from_service_account_json(credentials_path) if credentials_path else storage.Client()
    except Exception as e:
        return f"Error initializing GCS client: {e}"

    try:
        if action == "list":
            blobs = client.list_blobs(bucket, prefix=prefix, max_results=min(kwargs.get("max_keys", 100), 1000))
            out = []
            for blob in blobs:
                size = blob.size or 0
                size_str = f"{size}B" if size < 1024 else f"{size / 1024:.1f}KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f}MB"
                out.append(f"{blob.name:60s} {size_str:>8s}  {blob.updated.strftime('%Y-%m-%d %H:%M') if blob.updated else ''}")
            if not out:
                return f"No objects found in gs://{bucket}/{prefix}"
            return f"gs://{bucket}/ (showing {len(out)} objects):\n" + "\n".join(out)

        elif action == "upload":
            if not local_path:
                return "Error: 'local_path' is required for upload."
            if not os.path.isfile(local_path):
                return f"Error: Local file not found: {local_path}"
            blob = client.bucket(bucket).blob(key or os.path.basename(local_path))
            blob.upload_from_filename(local_path)
            if kwargs.get("public"):
                blob.make_public()
            dest_key = key or os.path.basename(local_path)
            return f"File uploaded to gs://{bucket}/{dest_key}"

        elif action == "download":
            if not key:
                return "Error: 'key' is required for download."
            dest = local_path or os.path.basename(key)
            os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
            blob = client.bucket(bucket).blob(key)
            blob.download_to_filename(dest)
            return f"File downloaded: gs://{bucket}/{key} -> {os.path.abspath(dest)}"

        elif action == "delete":
            if not key:
                return "Error: 'key' is required for delete."
            blob = client.bucket(bucket).blob(key)
            blob.delete()
            return f"Deleted gs://{bucket}/{key}"

        elif action == "info":
            if not key:
                return "Error: 'key' is required for info."
            blob = client.bucket(bucket).blob(key)
            # reload() triggers a network round-trip so metadata fields
            # below are populated rather than holding their constructor
            # defaults.
            blob.reload()
            return json.dumps({
                "key": blob.name,
                "size": blob.size,
                "content_type": blob.content_type,
                "updated": str(blob.updated),
                "generation": blob.generation,
                "md5_hash": blob.md5_hash,
                "metadata": blob.metadata or {},
            }, indent=2)

        elif action == "list_buckets":
            buckets = list(client.list_buckets())
            out = [f"{b.name:40s} {b.time_created.strftime('%Y-%m-%d') if b.time_created else ''}" for b in buckets]
            return "GCS Buckets:\n" + "\n".join(out) if out else "No buckets found."

        else:
            return f"Error: Unknown GCS action '{action}'. Options: list, upload, download, delete, info, list_buckets."

    except NotFound as e:
        return f"GCS error: {e}"
    except Exception as e:
        return f"GCS error: {e}"


async def _azure_action(action: str, container: str, blob_name: str, local_path: str, prefix: str, kwargs: dict[str, Any]) -> str:
    """Handle an action against Azure Blob Storage.

    Args:
        action: One of ``list`` / ``upload`` / ``download`` / ``delete`` /
            ``info``. Azure does not expose a bucket-listing action here
            because the SDK paginates containers lazily and the tool keeps
            a minimal surface.
        container: Target container name (analogous to a bucket).
        blob_name: Blob name (path within the container). Required for
            download/delete/info; optional for upload.
        local_path: Local file (upload) or destination (download).
        prefix: Blob name prefix used to scope ``list`` results.
        kwargs: Additional overrides -- ``connection_string``, ``max_keys``.

    The SDK authenticates through a connection string rather than a set
    of separate keys, so we require ``connection_string`` /
    ``AZURE_STORAGE_CONNECTION_STRING`` up front to give the caller a
    clean error message.
    """
    from azure.storage.blob import BlobServiceClient

    conn_str = kwargs.get("connection_string") or os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    if not conn_str:
        return "Error: Azure connection string required. Set AZURE_STORAGE_CONNECTION_STRING env var."

    try:
        service = BlobServiceClient.from_connection_string(conn_str)
        blob_service = service.get_container_client(container)
    except Exception as e:
        return f"Error initializing Azure client: {e}"

    try:
        if action == "list":
            # Azure's list_blobs returns a paginated generator; we stop
            # early once we reach max_results rather than paginating
            # through the entire prefix.
            max_results = min(kwargs.get("max_keys", 100), 1000)
            blobs = blob_service.list_blobs(name_starts_with=prefix)
            out = []
            count = 0
            for blob in blobs:
                if count >= max_results:
                    break
                size = blob.size or 0
                size_str = f"{size}B" if size < 1024 else f"{size / 1024:.1f}KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f}MB"
                out.append(f"{blob.name:60s} {size_str:>8s}")
                count += 1
            if not out:
                return f"No blobs in azure://{container}/{prefix}"
            return f"azure://{container}/ (showing {count} blobs):\n" + "\n".join(out)

        elif action == "upload":
            if not local_path:
                return "Error: 'local_path' is required for upload."
            if not os.path.isfile(local_path):
                return f"Error: Local file not found: {local_path}"
            blob_client = blob_service.get_blob_client(blob_name or os.path.basename(local_path))
            with open(local_path, "rb") as f:
                # overwrite=True replaces an existing blob of the same
                # name rather than raising a 409, matching S3 semantics.
                blob_client.upload_blob(f, overwrite=True)
            dest_key = blob_name or os.path.basename(local_path)
            return f"File uploaded to azure://{container}/{dest_key}"

        elif action == "download":
            if not blob_name:
                return "Error: 'key' is required for download."
            dest = local_path or os.path.basename(blob_name)
            os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
            blob_client = blob_service.get_blob_client(blob_name)
            with open(dest, "wb") as f:
                # readall() loads the entire blob into memory; adequate
                # for typical tool payloads but callers dealing with very
                # large objects should switch to streaming chunks.
                f.write(blob_client.download_blob().readall())
            return f"File downloaded: azure://{container}/{blob_name} -> {os.path.abspath(dest)}"

        elif action == "delete":
            if not blob_name:
                return "Error: 'key' is required for delete."
            blob_client = blob_service.get_blob_client(blob_name)
            blob_client.delete_blob()
            return f"Deleted azure://{container}/{blob_name}"

        elif action == "info":
            if not blob_name:
                return "Error: 'key' is required for info."
            blob_client = blob_service.get_blob_client(blob_name)
            props = blob_client.get_blob_properties()
            return json.dumps({
                "name": blob_name,
                "size": props.size,
                "content_type": props.content_settings.content_type if props.content_settings else "",
                "created": str(props.creation_time),
                "last_modified": str(props.last_modified),
                "etag": props.etag,
                "metadata": props.metadata or {},
            }, indent=2)

        else:
            return f"Error: Unknown Azure action '{action}'. Options: list, upload, download, delete, info."

    except Exception as e:
        return f"Azure error: {e}"


cloud_storage_tool = build_tool(
    name="cloud_storage",
    description=(
        "Cloud object-storage operations for AWS S3, Google Cloud Storage "
        "(GCS), and Azure Blob Storage behind one interface. Actions vary by "
        "provider: list (list objects, optionally prefix-filtered), upload "
        "(upload a local file), download (download an object to a local "
        "path), delete (delete an object), info (object metadata), "
        "list_buckets (list all buckets/containers, S3/GCS only), sync_up "
        "(sync a local directory to S3, S3 only). Use this instead of "
        "shelling out to aws/gsutil/az CLIs -- it parses responses into "
        "structured output and resolves credentials from the environment. "
        "TIP: Use 'prefix' with action='list' to scope large buckets and "
        "avoid huge responses. "
        "AVOID: Passing access_key/secret_key/connection_string inline in "
        "shared sessions -- prefer environment variables for credentials."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "enum": ["s3", "gcs", "azure"],
                "description": "Cloud storage provider (optional, default 's3').",
            },
            "action": {
                "type": "string",
                "description": "Operation (required): list, upload, download, delete, info, list_buckets, sync_up. Availability varies by provider.",
            },
            "bucket": {
                "type": "string",
                "description": "S3 bucket, GCS bucket, or Azure container name (required).",
            },
            "key": {
                "type": "string",
                "description": "Object key/path in cloud storage (required for download/delete/info; optional for upload, defaults to the local filename).",
            },
            "local_path": {
                "type": "string",
                "description": "Local file path for upload/download (required for upload and sync_up; optional for download, defaults to the object key basename).",
            },
            "prefix": {
                "type": "string",
                "description": "Prefix filter for listing objects (optional, used with action='list').",
            },
            "region": {
                "type": "string",
                "description": "AWS region (optional, default 'us-east-1' or the AWS_REGION/AWS_DEFAULT_REGION env var).",
            },
            "access_key": {
                "type": "string",
                "description": "AWS access key ID (optional, overrides the AWS_ACCESS_KEY_ID env var).",
            },
            "secret_key": {
                "type": "string",
                "description": "AWS secret access key (optional, overrides the AWS_SECRET_ACCESS_KEY env var).",
            },
            "credentials_path": {
                "type": "string",
                "description": "Path to a GCS service account JSON key (optional, overrides the GOOGLE_APPLICATION_CREDENTIALS env var).",
            },
            "connection_string": {
                "type": "string",
                "description": "Azure Storage connection string (optional, overrides the AZURE_STORAGE_CONNECTION_STRING env var).",
            },
            "public": {
                "type": "boolean",
                "description": "Make the uploaded object publicly readable (optional, used with action='upload').",
            },
            "max_keys": {
                "type": "integer",
                "description": "Max results to return for action='list' (optional, default 100, max 1000).",
            },
        },
        "required": ["action", "bucket"],
    },
    execute=_cloud_storage_execute,
    intents=["coding", "system", "data"],
    category="infra",
    semantic_type="network",
    # Read-only actions (list / info / list_buckets) share server-side
    # resources safely; mutations and directory syncs must be serialised.
    is_concurrency_safe=lambda data: data.get("action") in (
        "list", "info", "list_buckets",
    ),
    is_destructive=lambda args: args.get("action", "") in ("delete", "sync_up"),
)
