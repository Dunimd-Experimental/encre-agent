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

"""Platform API tools for Files, Batch, and Fine-tuning.

This module wraps the OpenAI-compatible platform APIs that the
configured Encre Agent backend exposes:

* ``file_api`` -- upload a local JSONL file, list/retrieve/delete
  previously uploaded files. Used to stage the input file that
  ``batch_api`` and ``fine_tune_api`` consume.
* ``batch_api`` -- create, retrieve, and list asynchronous batch jobs
  that process thousands of requests off-line within a 24-hour
  completion window.
* ``fine_tune_api`` -- start and list fine-tuning jobs built from an
  uploaded training JSONL file.

Design notes
------------

All three tool families go through a single ``_get_backend()``
resolver that walks the current runtime to find a backend object with
the relevant method. The ``hasattr`` gate is repeated per call
(``if backend is None or not hasattr(backend, "upload_file")``) rather
than centralised because it lets each action return a specific error
string -- the tool registry wants a diagnostic, not an exception, when
a backend is missing one of the platform APIs it advertises.

Response serialisation is uniform: every branch returns
``json.dumps({...}, indent=2, default=str)`` with a small curated
subset of the response object's fields (``id``, ``status``, ``filename``,
``bytes``, ``purpose``, ``deleted``). Curating the fields keeps the
tool result small enough for the model to read and stable across
backend version drift -- newer backends that add fields do not leak
them into the transcript unless we deliberately widen the projection.
The ``default=str`` fallback handles the case where a backend returns
an object that does not expose an ``id`` attribute.

The top-level ``execute=lambda`` dispatchers are written as one-line
ternary chains rather than if/elif blocks to match the existing style
of the tool; the branching cost is negligible and keeping them inline
means the schema and dispatch co-locate in the ``build_tool`` call.
"""

import json
from typing import Any

from encre.tools.base import build_tool


def _get_backend():
    """Resolve the currently-active backend instance from the runtime loop.

    Called at tool-execution time rather than at import time so the
    backend can be swapped out (e.g. by tests or by session reloads)
    without reimporting this module. The resolution is deferred to
    ``_resolve_loop`` from ``encre.tools.runtime``; if no loop is
    active or the loop's backend does not expose an
    ``upload_file`` method, we return ``None`` and each action
    handler produces a specific diagnostic string.

    Returns:
        The active backend object, or ``None`` when no backend is
        currently attached to the runtime loop.
    """
    from encre.tools.runtime import _resolve_loop
    loop = _resolve_loop()
    if loop and loop.backend and hasattr(loop.backend, "upload_file"):
        return loop.backend
    return None


async def _file_upload_execute(**kwargs: Any) -> str:
    """Upload a local file to the platform Files API.

    The backend's ``upload_file`` call is awaited directly: the
    backend already handles multipart encoding and network I/O so
    this wrapper stays thin. The response is projected to a small
    dict of scalar fields to keep the tool result stable across
    backend version drift.

    Args:
        kwargs: Tool input. Requires ``action == "upload"``, plus
            ``file`` (absolute path or base64 payload) and an
            optional ``purpose`` field.

    Returns:
        JSON-encoded upload receipt, or a diagnostic string when the
        backend does not implement the Files API.
    """
    backend = _get_backend()
    if backend is None or not hasattr(backend, "upload_file"):
        return "Error: Backend does not support Files API"
    try:
        file = kwargs.get("file", "")
        purpose = kwargs.get("purpose", "assistants")
        result = await backend.upload_file(file=file, purpose=purpose)
        return json.dumps({"id": result.id, "filename": result.filename, "bytes": result.bytes, "purpose": result.purpose} if hasattr(result, "id") else result, indent=2, default=str)
    except Exception as e:
        return f"Error uploading file: {e}"


async def _file_list_execute(**kwargs: Any) -> str:
    """List all previously uploaded files on the platform Files API.

    Returns a compact projection (``id``, ``filename``, ``bytes``) of
    every entry in the returned list; extra fields the backend may
    attach are intentionally dropped to keep the tool result terse.

    Args:
        kwargs: Tool input. Requires ``action == "list"``; no other
            fields are read.

    Returns:
        JSON-encoded list of uploaded files, or a diagnostic string
        when the backend does not implement the Files API.
    """
    backend = _get_backend()
    if backend is None or not hasattr(backend, "list_files"):
        return "Error: Backend does not support Files API"
    try:
        result = await backend.list_files()
        files = [{"id": f.id, "filename": f.filename, "bytes": f.bytes} for f in result.data]
        return json.dumps(files, indent=2, default=str)
    except Exception as e:
        return f"Error listing files: {e}"


async def _file_retrieve_execute(**kwargs: Any) -> str:
    """Retrieve metadata for a single uploaded file by id.

    Used primarily for confirming that a staged file is still present
    before starting a batch or fine-tuning job that references it.
    The projection is limited to ``id`` and ``filename`` -- other
    fields (``bytes``, ``purpose``, ``created_at``) are available
    from the ``list`` action.

    Args:
        kwargs: Tool input. Requires ``action == "retrieve"`` and
            ``file_id``.

    Returns:
        JSON-encoded file metadata, or a diagnostic string when the
        backend does not implement the Files API.
    """
    backend = _get_backend()
    if backend is None or not hasattr(backend, "retrieve_file"):
        return "Error: Backend does not support Files API"
    try:
        file_id = kwargs.get("file_id", "")
        result = await backend.retrieve_file(file_id=file_id)
        return json.dumps({"id": result.id, "filename": result.filename} if hasattr(result, "id") else result, indent=2, default=str)
    except Exception as e:
        return f"Error retrieving file: {e}"


async def _file_delete_execute(**kwargs: Any) -> str:
    """Delete an uploaded file by id.

    Files referenced by an in-flight batch or fine-tuning job cannot
    be deleted safely; the backend is expected to return an error
    payload that this wrapper surfaces as a normal ``Error deleting
    file: ...`` string.

    Args:
        kwargs: Tool input. Requires ``action == "delete"`` and
            ``file_id``.

    Returns:
        JSON-encoded deletion receipt (``{"deleted": true}``), or a
        diagnostic string when the backend does not implement the
        Files API.
    """
    backend = _get_backend()
    if backend is None or not hasattr(backend, "delete_file"):
        return "Error: Backend does not support Files API"
    try:
        file_id = kwargs.get("file_id", "")
        result = await backend.delete_file(file_id=file_id)
        return json.dumps({"deleted": result.deleted} if hasattr(result, "deleted") else result, indent=2, default=str)
    except Exception as e:
        return f"Error deleting file: {e}"


async def _batch_create_execute(**kwargs: Any) -> str:
    """Submit a new asynchronous batch job from an uploaded JSONL file.

    The backend queues the job and returns an ``id`` immediately;
    callers should poll ``retrieve`` to observe the ``status`` field.
    ``endpoint`` and ``completion_window`` are passed through as-is
    because the schema is defined by the target API rather than by
    this tool.

    Args:
        kwargs: Tool input. Requires ``action == "create"`` and
            ``input_file_id``; ``endpoint`` and
            ``completion_window`` have sensible defaults
            (``"/v1/chat/completions"`` and ``"24h"``).

    Returns:
        JSON-encoded batch receipt (``{"id": ..., "status": ...}``),
        or a diagnostic string when the backend does not implement
        the Batch API.
    """
    backend = _get_backend()
    if backend is None or not hasattr(backend, "create_batch"):
        return "Error: Backend does not support Batch API"
    try:
        input_file_id = kwargs.get("input_file_id", "")
        endpoint = kwargs.get("endpoint", "/v1/chat/completions")
        completion_window = kwargs.get("completion_window", "24h")
        result = await backend.create_batch(input_file_id=input_file_id, endpoint=endpoint, completion_window=completion_window)
        return json.dumps({"id": result.id, "status": result.status} if hasattr(result, "id") else result, indent=2, default=str)
    except Exception as e:
        return f"Error creating batch: {e}"


async def _batch_retrieve_execute(**kwargs: Any) -> str:
    """Retrieve the current status of a batch job by id.

    Polling is intentionally kept to a small subset of fields (``id``
    and ``status``) so a caller can loop cheaply without dragging the
    full batch object (which can be large) into the transcript.

    Args:
        kwargs: Tool input. Requires ``action == "retrieve"`` and
            ``batch_id``.

    Returns:
        JSON-encoded batch status, or a diagnostic string when the
        backend does not implement the Batch API.
    """
    backend = _get_backend()
    if backend is None or not hasattr(backend, "retrieve_batch"):
        return "Error: Backend does not support Batch API"
    try:
        batch_id = kwargs.get("batch_id", "")
        result = await backend.retrieve_batch(batch_id=batch_id)
        return json.dumps({"id": result.id, "status": result.status} if hasattr(result, "id") else result, indent=2, default=str)
    except Exception as e:
        return f"Error retrieving batch: {e}"


async def _batch_list_execute(**kwargs: Any) -> str:
    """List all batch jobs currently visible on the backend.

    Returns a compact projection (``id``, ``status``) for each job so
    the model can decide which batch to poll without pulling every
    full batch object into context.

    Args:
        kwargs: Tool input. Requires ``action == "list"``; no other
            fields are read.

    Returns:
        JSON-encoded list of batch jobs, or a diagnostic string when
        the backend does not implement the Batch API.
    """
    backend = _get_backend()
    if backend is None or not hasattr(backend, "list_batches"):
        return "Error: Backend does not support Batch API"
    try:
        result = await backend.list_batches()
        batches = [{"id": b.id, "status": b.status} for b in result.data]
        return json.dumps(batches, indent=2, default=str)
    except Exception as e:
        return f"Error listing batches: {e}"


async def _finetune_create_execute(**kwargs: Any) -> str:
    """Start a new fine-tuning job from an uploaded training JSONL file.

    ``training_file`` must reference a ``file_id`` produced by
    ``file_api upload`` with ``purpose="fine-tune"``; the backend
    validates the pairing and returns an error otherwise. ``model``
    must be a base model the backend permits fine-tuning on -- this
    tool does not re-validate the model name because the backend is
    the authoritative source.

    Args:
        kwargs: Tool input. Requires ``action == "create"`` and both
            ``training_file`` and ``model``.

    Returns:
        JSON-encoded fine-tuning job receipt (``{"id": ...,
        "status": ...}``), or a diagnostic string when the backend
        does not implement the Fine-tuning API.
    """
    backend = _get_backend()
    if backend is None or not hasattr(backend, "create_fine_tuning_job"):
        return "Error: Backend does not support Fine-tuning API"
    try:
        training_file = kwargs.get("training_file", "")
        model = kwargs.get("model", "")
        result = await backend.create_fine_tuning_job(training_file=training_file, model=model)
        return json.dumps({"id": result.id, "status": result.status} if hasattr(result, "id") else result, indent=2, default=str)
    except Exception as e:
        return f"Error creating fine-tuning job: {e}"


async def _finetune_list_execute(**kwargs: Any) -> str:
    """List all fine-tuning jobs visible on the backend.

    Returns only ``id`` and ``status`` so a caller can poll cheaply
    without pulling full job records (which include training logs and
    hyperparameter dumps) into the transcript.

    Args:
        kwargs: Tool input. Requires ``action == "list"``; no other
            fields are read.

    Returns:
        JSON-encoded list of fine-tuning jobs, or a diagnostic string
        when the backend does not implement the Fine-tuning API.
    """
    backend = _get_backend()
    if backend is None or not hasattr(backend, "list_fine_tuning_jobs"):
        return "Error: Backend does not support Fine-tuning API"
    try:
        result = await backend.list_fine_tuning_jobs()
        jobs = [{"id": j.id, "status": j.status} for j in result.data]
        return json.dumps(jobs, indent=2, default=str)
    except Exception as e:
        return f"Error listing fine-tuning jobs: {e}"


file_api_tool = build_tool(
    name="file_api",
    description=(
        "WHAT: Upload, list, retrieve, or delete files via the OpenAI-compatible "
        "Files API on the configured backend. Returns file IDs that other endpoints "
        "(batch, fine-tuning, assistants) accept. "
        "WHEN: Use to stage a JSONL input file for Batch API jobs, attach training "
        "data for fine-tuning, or manage previously uploaded files. "
        "WHEN NOT: Not for reading local files (use file_read) or for general HTTP "
        "uploads (use rest_client). The backend must implement upload_file / "
        "list_files / retrieve_file / delete_file or the call returns an error. "
        "TIPS: For 'upload' pass an absolute local path in 'file'; 'purpose' "
        "defaults to 'assistants' -- use 'batch' or 'fine-tune' when staging for "
        "those APIs. "
        "PITFALLS: Each action requires its own parameters (file for upload, file_id "
        "for retrieve/delete); mixing them silently no-ops. Large uploads may be "
        "subject to the backend's per-file size limits."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["upload", "list", "retrieve", "delete"], "description": "File operation: 'upload' a local file, 'list' all uploaded files, 'retrieve' metadata for one file_id, or 'delete' a file_id."},
            "file": {"type": "string", "description": "Absolute local path (or base64 data) of the file to upload. Required for action='upload'."},
            "file_id": {"type": "string", "description": "ID of the file to retrieve or delete (as returned by 'upload' or 'list'). Required for action='retrieve' and action='delete'."},
            "purpose": {"type": "string", "description": "Intended use of the uploaded file -- one of 'assistants' (default), 'batch', 'fine-tune', or 'vision'. Must match what the downstream endpoint expects.", "default": "assistants"},
        },
        "required": ["action"],
    },
    # Inline dispatch chain: keeps the schema and routing next to each
    # other in the build_tool call so a reader can see both together.
    execute=lambda **kw: _file_upload_execute(**kw) if kw.get("action") == "upload" else (_file_list_execute(**kw) if kw.get("action") == "list" else (_file_retrieve_execute(**kw) if kw.get("action") == "retrieve" else _file_delete_execute(**kw))),
    intents=["general", "data"],
    category="data",
    # Files API reads/writes are independent across calls; the backend
    # itself serialises concurrent mutations, so this tool is safe to
    # interleave.
    is_concurrency_safe=True,
)

batch_api_tool = build_tool(
    name="batch_api",
    description=(
        "WHAT: Create, retrieve, or list asynchronous batch processing jobs against "
        "the OpenAI-compatible Batch API. Batches run large volumes of requests "
        "off-line at lower cost. "
        "WHEN: Use when you have a JSONL file of requests (already uploaded via "
        "file_api) and want to process thousands of completions asynchronously "
        "within a 24h window. "
        "WHEN NOT: Not for interactive single requests -- call the model directly. "
        "The backend must implement create_batch / retrieve_batch / list_batches or "
        "the call returns an error. "
        "TIPS: Set 'endpoint' to match the API version of your JSONL requests "
        "('/v1/chat/completions' by default); 'completion_window' is usually '24h'. "
        "PITFALLS: Polling retrieve too frequently is wasteful -- batches typically "
        "take minutes to hours; check status a few times per window."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "retrieve", "list"], "description": "Batch operation: 'create' submits a new batch from an input file, 'retrieve' fetches the status of one batch_id, 'list' enumerates all batches."},
            "input_file_id": {"type": "string", "description": "file_id of the JSONL input file (uploaded via file_api with purpose='batch'). Required for action='create'."},
            "endpoint": {"type": "string", "description": "Target API endpoint for each request in the batch -- typically '/v1/chat/completions' or '/v1/embeddings'. Required for action='create'.", "default": "/v1/chat/completions"},
            "completion_window": {"type": "string", "description": "Maximum time the backend has to complete the batch, e.g. '24h'. Most backends only support '24h'.", "default": "24h"},
            "batch_id": {"type": "string", "description": "ID of the batch to retrieve (as returned by 'create' or 'list'). Required for action='retrieve'."},
        },
        "required": ["action"],
    },
    # Inline dispatch chain, same rationale as file_api above.
    execute=lambda **kw: _batch_create_execute(**kw) if kw.get("action") == "create" else (_batch_retrieve_execute(**kw) if kw.get("action") == "retrieve" else _batch_list_execute(**kw)),
    intents=["general", "data"],
    category="data",
    # Batch operations are stateless from this tool's perspective; the
    # backend serialises its own writes, so concurrent dispatches are
    # safe.
    is_concurrency_safe=True,
)

fine_tune_api_tool = build_tool(
    name="fine_tuning_api",
    description=(
        "WHAT: Create a fine-tuning job from an uploaded training file, or list "
        "existing fine-tuning jobs and their statuses, via the OpenAI-compatible "
        "Fine-tuning API. "
        "WHEN: Use after uploading a properly formatted JSONL training file via "
        "file_api, when you need a model specialised for your domain or task. "
        "WHEN NOT: Not for quick experimentation -- fine-tuning is slow, expensive, "
        "and irreversible; prefer prompt engineering or retrieval first. The backend "
        "must implement create_fine_tuning_job / list_fine_tuning_jobs or the call "
        "errors. "
        "TIPS: 'training_file' must be a file_id returned by file_api 'upload' with "
        "purpose='fine-tune'; 'model' must be a base model the backend allows "
        "fine-tuning. "
        "PITFALLS: Jobs can take hours and may fail mid-run -- poll list "
        "periodically and validate the training file format before submitting to "
        "avoid wasted quota."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "list"], "description": "Fine-tuning operation: 'create' starts a new job from a training file, 'list' enumerates all jobs and their statuses."},
            "training_file": {"type": "string", "description": "file_id of the JSONL training data (uploaded via file_api with purpose='fine-tune'). Required for action='create'."},
            "model": {"type": "string", "description": "Name of the base model to fine-tune (e.g. 'gpt-4o', 'gpt-3.5-turbo'). Must be a model the backend allows fine-tuning. Required for action='create'."},
        },
        "required": ["action"],
    },
    # Inline dispatch chain, same rationale as the file and batch tools.
    execute=lambda **kw: _finetune_create_execute(**kw) if kw.get("action") == "create" else _finetune_list_execute(**kw),
    intents=["general", "data"],
    category="data",
    # Same reasoning as the batch tool: stateless from the tool's
    # perspective, backend owns serialisation.
    is_concurrency_safe=True,
)

__all__ = ["file_api_tool", "batch_api_tool", "fine_tune_api_tool"]
