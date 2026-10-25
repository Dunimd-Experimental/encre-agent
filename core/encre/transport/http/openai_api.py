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
OpenAI-compatible HTTP API server platform adapter.

Module position inside Encre
-----------------------------
This is the HTTP wire surface of the Python :mod:`encre` package, sitting in the
same layer as the WebSocket transports (:mod:`encre.transport.ws`,
) and alongside
:class:`encre.transport.http.channel.HTTPChannel`. It is the only entry point
that lets a third-party client talk to the Encre agent core in the wire format
those clients already speak, so the desktop renderer, headless drivers and the
gateway platforms all remain usable at the same time.

Surface implemented here
------------------------
Every path below is registered by :func:`APIServerAdapter._http_route_table`
and mirrored verbatim under the ``/p/<profile>/`` multiplex prefix:

* ``POST /v1/chat/completions`` - OpenAI Chat Completions format. Stateless by
  default; ``X-Encre-Session-Id`` opts a client into transcript continuity and
  ``X-Encre-Session-Key`` opts it into long-term memory scoping.
* ``POST /v1/responses`` - OpenAI Responses API format, stateful through
  ``previous_response_id``; ``X-Encre-Session-Key`` supported.
* ``GET`` / ``DELETE /v1/responses/{response_id}`` - retrieve or drop a stored
  response from the local :class:`ResponseStore`.
* ``GET /v1/models`` - advertise ``encre-agent`` plus any ``model_routes``
  aliases configured for this adapter.
* ``GET /v1/capabilities`` - machine-readable contract for external UIs.
* ``GET /v1/skills``, ``GET /v1/toolsets`` - read-only tool surface listings.
* ``/api/sessions`` (+ ``{session_id}``, ``/messages``, ``/fork``, ``/chat``,
  ``/chat/stream``) - thin CRUD over the ``SessionDB`` transcript store.
* ``/v1/runs`` (+ ``/{run_id}``, ``/{run_id}/events``, ``/{run_id}/approval``,
  ``/{run_id}/stop``) - asynchronous run submission with an SSE event feed.
* ``/api/jobs`` (+ ``/{job_id}``, ``/pause``, ``/resume``, ``/run``) - cron job
  management; ``POST /api/cron/fire`` is the Chronos NAS fire webhook.
* ``POST /api/platforms/{platform}/events`` - generic platform HTTP callback
  ingress, authenticated by the target adapter's own verifier.
* ``GET /health``, ``GET /health/detailed`` - liveness and rich readiness.

Any OpenAI-compatible frontend (Open WebUI, LobeChat, LibreChat, AnythingLLM,
NextChat, ChatBox, ...) can therefore point at
``http://localhost:8642/v1`` and authenticate with a bearer
``API_SERVER_KEY``.

When ``encre.gateway.multiplex_profiles`` is enabled, the default profile owns
this listener and every other profile is reachable through a URL prefix, using
the same contract as the webhook adapter:

    GET  /p/<profile>/v1/models
    POST /p/<profile>/v1/chat/completions
    ...

The profile-prefix middleware validates the prefix and scopes config and
credential reads to that profile for the lifetime of one request.

Requires:
- aiohttp (already available in the gateway)

Public API
----------
* :class:`APIAdapter` - the :class:`encre.gateway.platforms.base.BasePlatformAdapter`
  implementation; owns the aiohttp application, the response store, all run
  transport state and the background sweep task.
* :class:`ResponseStore` - SQLite-backed LRU store for Responses API state.
* :func:`cors_middleware`, :func:`body_limit_middleware`,
  :func:`security_headers_middleware` - application-level middleware.
* :func:`_admit_api_agent_request` - authentication plus drain gate that every
  agent-serving handler is wrapped with.
* :func:`check_api_server_requirements` - capability probe used by the gateway
  startup path.

Data flow
---------
An HTTP request is authenticated and drain-checked, profile-scoped, validated
and normalized to Encre's message shapes, routed to a
:class:`encre.server.session_manager.SessionManager`-backed agent created by
:func:`APIServerAdapter._create_agent`, and executed in a thread executor.
Agent deltas come back through callback queues; an SSE writer turns them into
OpenAI wire frames and the handler resolves the request with the translated
result.
"""

import asyncio
import errno
import hashlib
import hmac
import json
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# Uniquely identifies "a /p/<profile>/ prefix was supplied but this gateway does
# not serve that profile", which handlers and the profile middleware translate to
# a 404. It must stay distinct from ``None``, which instead means "no prefix, or
# multiplexing disabled", i.e. handle the request as the default profile.
_PROFILE_REJECTED = object()

# Profile name selected by the ``/p/<profile>/`` URL prefix for the request being
# handled. The profile-prefix middleware writes it at the top of the request, and
# handlers plus :func:`APIServerAdapter._run_agent` read it later. Read through a
# ContextVar rather than a request attribute because the agent actually runs in a
# worker thread via ``run_in_executor``; the value is captured on the event-loop
# thread and re-entered as an explicit argument there.
_api_request_profile: ContextVar[Optional[str]] = ContextVar(
    "api_server_request_profile", default=None
)

def _approval_event_choices(*, smart_denied: bool, allow_permanent: bool) -> list[str]:
    """Return the approval choices offered on an ``approval.request`` SSE event.

    ``allow_permanent`` mirrors whether the flagged tool permits a "always"
    grant for its own class of action, so a permanent approval can never be
    offered for a tool whose policy forbids it. When the request was already
    smart-denied by the tool policy layer, the only honest remaining actions
    are a single-use grant or an explicit refusal.

    Args:
        smart_denied: True when the tool policy already refused this action.
        allow_permanent: True when a permanent ("always") grant is valid for it.

    Returns:
        The ordered list of choice strings to advertise to the caller.
    """
    if smart_denied:
        return ["once", "deny"]
    return ["once", "session", "always", "deny"] if allow_permanent else ["once", "session", "deny"]


from aiohttp import web
AIOHTTP_AVAILABLE = True

from encre.gateway.config import Platform, PlatformConfig
from encre.auth import has_usable_secret
from encre.gateway.platforms.base import (
    MEDIA_TAG_CLEANUP_RE,
    BasePlatformAdapter,
    SendResult,
    is_network_accessible,
    validate_media_delivery_path,
)
def redact_sensitive_text(text, *a, **kw):
    """Return *text* unchanged; a local stand-in for the gateway redactor.

    The gateway connector adapters (now shipped as ``ea-connector-*``
    plugins) define the same identity stub, so call sites written against
    the gateway's redaction seam keep working when this transport module is
    imported on its own. The extra positional and keyword parameters are
    accepted and discarded purely for call-site signature compatibility.

    Args:
        text: The string a real redactor would scrub.
        *a: Unused positional arguments.
        **kw: Unused keyword arguments, e.g. ``force``.

    Returns:
        The input string, unmodified.
    """
    return text
from encre.gateway.readiness import collect_runtime_readiness

logger = logging.getLogger(__name__)


def _encre_version() -> str:
    """Return the installed Encre version string, or ``"dev"`` if unavailable.

    The string is surfaced by ``GET /health`` and ``GET /health/detailed`` so an
    external dashboard can correlate reported behaviour with a specific Encre
    build. Resolution goes through ``importlib.metadata`` rather than a hardcoded
    constant because the version is set by the packaging step in
    :file:`build.py` and may be absent in a source checkout.

    Returns:
        The distribution version for the ``encre`` package, or ``"dev"`` when the
        distribution is not installed (development tree, tests).
    """
    try:
        from importlib.metadata import version
        return version("encre")
    except Exception:
        return "dev"


# ---------------------------------------------------------------------------
# Default settings and defensive request-shape limits
# ---------------------------------------------------------------------------
# Loopback is the default bind so that the agent's terminal and file tools stay
# unreachable from the LAN unless an operator opts into an explicit address.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8642
# Response objects retained for the stateful Responses API. Each entry stores
# the full internal transcript (including tool payloads), so this is an LRU
# bound on entry count rather than bytes.
MAX_STORED_RESPONSES = 100
# Rejected up front by the body-limit middleware and enforced a second time by
# aiohttp's ``client_max_size``. Ten MB is well above any realistic tool-carrying
# turn, including image-heavy vision prompts.
MAX_REQUEST_BYTES = 10_000_000  # 10 MB, comfortably above any realistic tool-carrying turn
# SSE streams stay open for the whole agent turn, which easily exceeds this
# window while the model thinks or a long-running tool executes. A periodic
# comment frame keeps idle proxies and load balancers from severing the socket.
CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS = 30.0
# Bounds applied while flattening client-supplied content parts: each part and
# the accumulated result are truncated here so a hostile payload cannot drive
# unbounded allocation.
MAX_NORMALIZED_TEXT_LENGTH = 65_536  # 65536 chars (64 KB) cap for normalized content parts
# Number of entries in a content array that are considered before normalization
# stops; anything beyond this is dropped rather than processed.
MAX_CONTENT_LIST_SIZE = 1_000  # Max items when content is an array


def _coerce_port(value: Any, default: int = DEFAULT_PORT) -> int:
    """Parse a listen port without letting a malformed config value crash startup.

    ``config.yaml`` and ``API_SERVER_PORT`` are both operator-supplied and may
    hold a non-numeric placeholder or a stale fragment. Failing the entire
    gateway over one platform's typo is worse than silently using the historical
    default, which is then surfaced loudly by the bind attempt in
    :func:`APIServerAdapter.connect`.

    Args:
        value: Raw port string from config or environment.
        default: Fallback when *value* cannot be interpreted as an integer.

    Returns:
        An integer port suitable for ``web.TCPSite``.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Accepted spellings for string-valued booleans in request payloads. Kept as
# frozensets so membership tests on untrusted input stay O(1) and hash-free.
_TRUE_REQUEST_BOOL_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_REQUEST_BOOL_STRINGS = frozenset({"0", "false", "no", "off"})


def _coerce_request_bool(value: Any, default: bool = False) -> bool:
    """Normalize boolean-like API payload values.

    External clients should send real JSON booleans, but some OpenAI-compatible
    frontends and middleware serialize flags like ``stream`` as strings.  Using
    Python truthiness on those values misroutes requests because ``"false"`` is
    still truthy, which would flip a non-streaming call into an SSE response and
    make the client hang waiting for a JSON body. Only explicit bool-ish scalars
    are treated as booleans; everything else falls back to the caller's default.

    Args:
        value: The raw payload value of a boolean flag.
        default: Returned when *value* is ``None`` or an unrecognized string.

    Returns:
        The interpreted boolean.

    Note:
        Numeric inputs use truthiness so ``stream: 1`` from a loosely typed
        client still selects the streaming path, while ``stream: 0`` does not.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_REQUEST_BOOL_STRINGS:
            return True
        if normalized in _FALSE_REQUEST_BOOL_STRINGS:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _normalize_chat_content(
    content: Any, *, _max_depth: int = 10, _depth: int = 0,
) -> str:
    """Flatten OpenAI chat message content into a single plain text string.

    Some clients (Open WebUI, LobeChat, ...) send ``content`` as an array of
    typed parts instead of a plain string::

        [{"type": "text", "text": "hello"}, {"type": "input_text", "text": "..."}]

    The agent pipeline (``run_conversation`` on the ``AIAgent`` produced by
    :func:`APIServerAdapter._create_agent`) expects a string, so multi-part
    shapes are collapsed here. Non-text parts (image_url, file) are skipped
    rather than stringified, since a literal ``dict`` repr of an image part
    would be meaningless to the model; callers that need images go through
    :func:`_normalize_multimodal_content` instead.

    Defensive limits bound recursion depth, list size, per-part length, and the
    accumulated output length so a hostile payload cannot drive unbounded work.

    Args:
        content: Raw ``content`` field of one message.
        _max_depth: Recursion ceiling guarding against deeply nested lists.
        _depth: Current recursion level; passed positionally by the recursion.

    Returns:
        The flattened text, joined with newlines, or ``""`` for empty or
        unconvertible input.
    """
    if _depth > _max_depth:
        return ""
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content

    if isinstance(content, list):
        parts: List[str] = []
        total_len = 0
        items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
        for item in items:
            if isinstance(item, str):
                if item:
                    part = item[:MAX_NORMALIZED_TEXT_LENGTH]
                    parts.append(part)
                    total_len += len(part)
            elif isinstance(item, dict):
                item_type = str(item.get("type") or "").strip().lower()
                if item_type in {"text", "input_text", "output_text"}:
                    text = item.get("text", "")
                    if text:
                        try:
                            part = str(text)[:MAX_NORMALIZED_TEXT_LENGTH]
                            parts.append(part)
                            total_len += len(part)
                        except Exception:
                            pass
                # Ignore image_url and any other non-text part: this helper is
                # the text-only path, and stringifying an image dict would put
                # noise in the model prompt. Multimodal turns use
                # _normalize_multimodal_content, which preserves them.
            elif isinstance(item, list):
                nested = _normalize_chat_content(item, _max_depth=_max_depth, _depth=_depth + 1)
                if nested:
                    parts.append(nested)
                    total_len += len(nested)
            # Stop early once the accumulated text hits the cap, so a
            # thousands-of-parts payload cannot force a full scan.
            if total_len >= MAX_NORMALIZED_TEXT_LENGTH:
                break
        result = "\n".join(parts)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result

    # Last resort for unexpected scalars (int, float, bool). Converting rather
    # than returning empty keeps a client that sends content: 42 from failing
    # the "no user message" check instead of getting a confusing 400.
    try:
        result = str(content)
        return result[:MAX_NORMALIZED_TEXT_LENGTH] if len(result) > MAX_NORMALIZED_TEXT_LENGTH else result
    except Exception:
        return ""


# Content part type aliases used by the OpenAI Chat Completions and Responses
# APIs. Both spellings of each concept are accepted on the way in and emitted in
# one canonical internal shape (``{"type": "text", ...}`` /
# ``{"type": "image_url", ...}``) that the rest of the agent pipeline already
# understands. Chat Completions and the Responses API name the same concepts
# differently, so the sets are the single place where that divergence is absorbed.
_TEXT_PART_TYPES = frozenset({"text", "input_text", "output_text"})
_IMAGE_PART_TYPES = frozenset({"image_url", "input_image"})
_FILE_PART_TYPES = frozenset({"file", "input_file"})


def _normalize_multimodal_content(content: Any) -> Any:
    """Validate and normalize multimodal content for the API server.

    Returns a plain string when the content is text-only, or a list of
    ``{"type": "text"|"image_url", ...}`` parts when images are present.
    The output shape is the native OpenAI Chat Completions vision format,
    which the agent pipeline accepts verbatim (OpenAI-wire providers) or
    converts (``_preprocess_anthropic_content`` for Anthropic).

    Unlike :func:`_normalize_chat_content`, this validator rejects anything it
    cannot faithfully represent instead of silently dropping it, because a
    silently lost image is indistinguishable from a client that sent text
    only.

    Args:
        content: Raw ``content`` field of one message, in either Chat
            Completions or Responses spelling.

    Returns:
        A ``str`` for text-only input, or ``list[dict]`` when image parts are
        present. ``""`` when the input carries no visible payload.

    Raises:
        ValueError: Carries an ``"<code>:<message>"`` payload understood by
            :func:`_multimodal_validation_error`:
            * ``unsupported_content_type`` - file/input_file parts, or a
              non-image ``data:`` URL.
            * ``invalid_image_url`` - missing URL or unsupported scheme.
            * ``invalid_content_part`` - malformed text or image object.

    Note:
        Only ``http(s)://`` URLs and ``data:image/...`` payloads are accepted
        for images. ``file://`` and bare local paths are refused because the
        caller controls them and the server would otherwise fetch arbitrary
        local files on the caller's behalf.
    """
    # Scalar passthrough mirrors ``_normalize_chat_content``.
    if content is None:
        return ""
    if isinstance(content, str):
        return content[:MAX_NORMALIZED_TEXT_LENGTH] if len(content) > MAX_NORMALIZED_TEXT_LENGTH else content
    if not isinstance(content, list):
        # Mirror the legacy text-normalizer's fallback so callers that
        # pre-existed image support still get a string back.
        return _normalize_chat_content(content)

    items = content[:MAX_CONTENT_LIST_SIZE] if len(content) > MAX_CONTENT_LIST_SIZE else content
    normalized_parts: List[Dict[str, Any]] = []
    text_accum_len = 0

    for part in items:
        if isinstance(part, str):
            if part:
                trimmed = part[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if not isinstance(part, dict):
            # Ignore unknown scalars for forward compatibility with future
            # Responses API additions (e.g. ``refusal``).  The same policy
            # the text normalizer applies.
            continue

        raw_type = part.get("type")
        part_type = str(raw_type or "").strip().lower()

        if part_type in _TEXT_PART_TYPES:
            text = part.get("text")
            if text is None:
                continue
            if not isinstance(text, str):
                text = str(text)
            if text:
                trimmed = text[:MAX_NORMALIZED_TEXT_LENGTH]
                normalized_parts.append({"type": "text", "text": trimmed})
                text_accum_len += len(trimmed)
            continue

        if part_type in _IMAGE_PART_TYPES:
            detail = part.get("detail")
            image_ref = part.get("image_url")
            # OpenAI Responses sends ``input_image`` with a top-level
            # ``image_url`` string; Chat Completions sends ``image_url`` as
            # ``{"url": "...", "detail": "..."}``.  Support both.
            if isinstance(image_ref, dict):
                url_value = image_ref.get("url")
                detail = image_ref.get("detail", detail)
            else:
                url_value = image_ref
            if not isinstance(url_value, str) or not url_value.strip():
                raise ValueError("invalid_image_url:Image parts must include a non-empty image URL.")
            url_value = url_value.strip()
            lowered = url_value.lower()
            if lowered.startswith("data:"):
                if not lowered.startswith("data:image/") or "," not in url_value:
                    raise ValueError(
                        "unsupported_content_type:Only image data URLs are supported. "
                        "Non-image data payloads are not supported."
                    )
            elif not (lowered.startswith("http://") or lowered.startswith("https://")):
                raise ValueError(
                    "invalid_image_url:Image inputs must use http(s) URLs or data:image/... URLs."
                )
            image_part: Dict[str, Any] = {"type": "image_url", "image_url": {"url": url_value}}
            if detail is not None:
                if not isinstance(detail, str) or not detail.strip():
                    raise ValueError("invalid_content_part:Image detail must be a non-empty string when provided.")
                image_part["image_url"]["detail"] = detail.strip()
            normalized_parts.append(image_part)
            continue

        if part_type in _FILE_PART_TYPES:
            raise ValueError(
                "unsupported_content_type:Inline image inputs are supported, "
                "but uploaded files and document inputs are not supported on this endpoint."
            )

        # Unknown part type: reject explicitly so the caller gets a clear 400
        # instead of a turn that quietly lost part of its input.
        raise ValueError(
            f"unsupported_content_type:Unsupported content part type {raw_type!r}. "
            "Only text and image_url/input_image parts are supported."
        )

    if not normalized_parts:
        return ""

    # Text-only: collapse to a plain string so downstream logging/trajectory
    # code sees the native shape and prompt caching on text-only turns is
    # unaffected.
    if all(p.get("type") == "text" for p in normalized_parts):
        return "\n".join(p["text"] for p in normalized_parts if p.get("text"))

    return normalized_parts


def _content_has_visible_payload(content: Any) -> bool:
    """Return whether *content* carries any text or image attachment.

    Used to reject empty turns before an agent is spun up. Whitespace-only
    text counts as empty because most frontends submit a blank content part on
    submit, and running a full agent turn for it would just burn model quota.

    Args:
        content: Normalized-or-raw message content.

    Returns:
        ``True`` when at least one part has non-blank text or an image.
    """
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                ptype = str(part.get("type") or "").strip().lower()
                if ptype in _TEXT_PART_TYPES and str(part.get("text") or "").strip():
                    return True
                if ptype in _IMAGE_PART_TYPES:
                    return True
    return False


def _multimodal_validation_error(exc: ValueError, *, param: str) -> "web.Response":
    """Translate a :func:`_normalize_multimodal_content` ValueError into a 400 response.

    The normalizer encodes the OpenAI error code in the exception message using
    the ``"<code>:<message>"`` convention, so this splits on the first colon and
    forwards both halves into the standard :func:`_openai_error` envelope.
    ``param`` tells the client which field was rejected, which the OpenAI SDK
    surfaces in its own error rendering.

    Args:
        exc: Exception raised by the normalizer.
        param: Request path of the offending field, e.g. ``messages[2].content``.

    Returns:
        A 400 JSON response using the OpenAI error envelope.
    """
    raw = str(exc)
    code, _, message = raw.partition(":")
    if not message:
        code, message = "invalid_content_part", raw
    return web.json_response(
        _openai_error(message, code=code, param=param),
        status=400,
    )


def _session_chat_user_message(body: Dict[str, Any], *, param: str = "message") -> tuple[Any, Optional["web.Response"]]:
    """Extract and normalize the user message of a session chat request.

    The session chat endpoints accept either ``message`` (the Encre-native name)
    or ``input`` (the Responses API name), so callers sharing code between the
    two surfaces work unmodified. Validation is identical to
    :func:`_handle_chat_completions`, including multimodal part handling, so a
    request that is valid on one endpoint is valid on the other.

    Args:
        body: Parsed JSON request body.
        param: Field name reported in validation errors.

    Returns:
        ``(normalized_content, None)`` on success, or ``(None, error_response)``
        when the message is missing, blank, or malformed.
    """
    user_message = body.get("message") or body.get("input")
    if not _content_has_visible_payload(user_message):
        return None, web.json_response(
            _openai_error("Missing 'message' field", code="missing_message"),
            status=400,
        )
    try:
        return _normalize_multimodal_content(user_message), None
    except ValueError as exc:
        return None, _multimodal_validation_error(exc, param=param)


def check_api_server_requirements() -> bool:
    """Report whether the optional aiohttp dependency is importable.

    Registered as the ``check_fn`` for the ``api_server`` platform entry in
    :mod:`encre.gateway.platform_registry`, so :class:`encre.gateway.run.GatewayRunner`
    can skip this adapter instead of failing startup when aiohttp is absent.
    aiohttp ships with the gateway extras in normal installs, so this is a
    soft capability probe rather than a hard requirement.

    Returns:
        ``True`` when the HTTP surface can be served.
    """
    return AIOHTTP_AVAILABLE


class ResponseStore:
    """
    SQLite-backed LRU store for Responses API state.

    Each stored response includes the full internal conversation history
    (with tool calls and results) so it can be reconstructed on subsequent
    requests via previous_response_id.

    Persists across gateway restarts.  Falls back to in-memory SQLite
    if the on-disk path is unavailable.

    Lifecycle
    ---------
    Constructed once per adapter in ``APIServerAdapter.__init__`` and closed in
    ``disconnect()``. Closing releases the database file and the WAL/SHM
    sidecars; leaking it leaks two file descriptors per reconnect attempt.

    Invariants
    ----------
    * At most ``max_size`` rows in the ``responses`` table at any time;
      eviction runs inline on every :meth:`put`.
    * Every ``conversations`` row points at an existing response row; both
      :meth:`put` eviction and :meth:`delete` remove stale mappings.

    Thread safety
    -------------
    The connection is opened with ``check_same_thread=False`` because the store
    is touched from aiohttp handler tasks and occasionally from worker threads.
    All calls are short single-statement transactions, so SQLite's own
    connection-level locking serialises them.
    """

    def __init__(self, max_size: int = MAX_STORED_RESPONSES, db_path: str = None):
        """Open the SQLite store and prepare its two tables.

        Falls back to an in-memory database when the on-disk path cannot be
        resolved or opened, so ``previous_response_id`` chaining degrades to
        process-local state instead of preventing the server from starting.

        Args:
            max_size: LRU capacity in rows; eviction runs inline on :meth:`put`.
            db_path: Explicit database path, or ``None`` to derive
                ``<data_dir>/response_store.db``.
        """
        self._max_size = max_size
        if db_path is None:
            try:
                db_path = str(get_data_dir() / "response_store.db")
            except Exception:
                db_path = ":memory:"
        self._db_path: Optional[str] = db_path if db_path != ":memory:" else None
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        except Exception:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._db_path = None
        # Use shared WAL-fallback helper so response_store.db degrades
        # gracefully on NFS/SMB/FUSE-mounted filesystem.
        from encre.storage import apply_wal_with_fallback
        apply_wal_with_fallback(self._conn, db_label="response_store.db")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                response_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                accessed_at REAL NOT NULL
            )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                name TEXT PRIMARY KEY,
                response_id TEXT NOT NULL
            )"""
        )
        self._conn.commit()
        # response_store.db contains conversation history (tool payloads,
        # prompts, results). Tighten to owner-only after creation so other
        # local users on a shared box cannot read it. Run once at __init__
        # rather than after every commit - chmod-on-every-write is wasted
        # syscalls on a hot path.
        self._tighten_file_permissions()

    def _tighten_file_permissions(self) -> None:
        """Force owner-only permissions on the DB and SQLite sidecars.

        SQLite writes three files: the database itself plus the ``-wal`` and
        ``-shm`` sidecars created when WAL mode is active. Any of them may hold
        prompt text, so all three need the same mode. The sidecars appear
        lazily, which is why each candidate is guarded by ``exists()`` and the
        calls are wrapped in ``try/except OSError``: on Windows, filesystems
        without permission bits, or a freshly rotated file this must never
        break request handling.
        """
        if not self._db_path:
            return
        for candidate in (
            Path(self._db_path),
            Path(f"{self._db_path}-wal"),
            Path(f"{self._db_path}-shm"),
        ):
            try:
                if candidate.exists():
                    candidate.chmod(0o600)
            except OSError:
                logger.debug(
                    "Failed to restrict response store permissions for %s",
                    candidate,
                    exc_info=True,
                )

    def get(self, response_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a stored response by ID (updates access time for LRU).

        Reading bumps ``accessed_at`` so a response that is still being chained
        via ``previous_response_id`` survives LRU eviction. The access-time
        write happens on a successful read only; a miss returns immediately
        without touching the database.

        Args:
            response_id: Response id previously returned by ``POST /v1/responses``.

        Returns:
            The stored payload dict, or ``None`` when the id is unknown or the
            stored JSON is corrupt (in which case the bad row is evicted).
        """
        row = self._conn.execute(
            "SELECT data FROM responses WHERE response_id = ?", (response_id,)
        ).fetchone()
        if row is None:
            return None
        self._conn.execute(
            "UPDATE responses SET accessed_at = ? WHERE response_id = ?",
            (time.time(), response_id),
        )
        self._conn.commit()
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Corrupted JSON in response store for id=%s, evicting entry",
                response_id,
            )
            self._conn.execute(
                "DELETE FROM responses WHERE response_id = ?",
                (response_id,),
            )
            self._conn.commit()
            return None

    def put(self, response_id: str, data: Dict[str, Any]) -> None:
        """Store a response, evicting the oldest if at capacity.

        Uses ``INSERT OR REPLACE`` so re-persisting a snapshot for the same
        response id (in-progress to completed) updates in place instead of
        allocating a new slot. Eviction is opportunistic and runs in the same
        transaction as the insert, so a caller can never observe a store that
        exceeds ``max_size``.

        Args:
            response_id: Response id that also identifies the conversation tail.
            data: Payload to persist; serialized with ``default=str`` so any
                non-JSON object still stores rather than raising mid-request.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO responses (response_id, data, accessed_at) VALUES (?, ?, ?)",
            (response_id, json.dumps(data, default=str), time.time()),
        )
        # Evict oldest entries beyond max_size
        count = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0]
        if count > self._max_size:
            # Collect IDs that will be evicted
            evict_ids = [
                row[0]
                for row in self._conn.execute(
                    "SELECT response_id FROM responses ORDER BY accessed_at ASC LIMIT ?",
                    (count - self._max_size,),
                ).fetchall()
            ]
            if evict_ids:
                placeholders = ",".join("?" for _ in evict_ids)
                # Clear conversation mappings pointing to evicted responses
                self._conn.execute(
                    f"DELETE FROM conversations WHERE response_id IN ({placeholders})",
                    evict_ids,
                )
                # Delete evicted responses
                self._conn.execute(
                    f"DELETE FROM responses WHERE response_id IN ({placeholders})",
                    evict_ids,
                )
        self._conn.commit()

    def delete(self, response_id: str) -> bool:
        """Remove a response from the store. Returns True if found and deleted.

        Deletes the ``conversations`` mappings first so a concurrent read cannot
        resolve a conversation name to a response that no longer exists, then
        deletes the response row itself. Both deletes are committed together.

        Args:
            response_id: Response id to drop.

        Returns:
            ``True`` if a response row existed and was removed, ``False`` when
            the id was already absent (handlers map that to a 404).
        """
        # Clear conversation mappings pointing to this response
        self._conn.execute(
            "DELETE FROM conversations WHERE response_id = ?", (response_id,)
        )
        cursor = self._conn.execute(
            "DELETE FROM responses WHERE response_id = ?", (response_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_conversation(self, name: str) -> Optional[str]:
        """Get the latest response_id for a conversation name.

        ``conversation`` is the caller-supplied conversation alias used instead
        of passing ``previous_response_id`` by hand; each ``POST /v1/responses``
        with the same alias rebinds this mapping, so a client can follow one
        named thread across restarts.

        Args:
            name: Conversation alias from the request body.

        Returns:
            The most recent response id for *name*, or ``None`` when the alias
            has never been used (treated by the handler as a new conversation).
        """
        row = self._conn.execute(
            "SELECT response_id FROM conversations WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row else None

    def set_conversation(self, name: str, response_id: str) -> None:
        """Map a conversation name to its latest response_id.

        Rebinding is intentionally unconditional: a conversation alias always
        points at the newest response, so ``get_conversation`` never needs an
        ordering clause. Callers pass the previous response's id back as
        ``previous_response_id`` for the actual history, so this table is a
        convenience index rather than the source of truth.

        Args:
            name: Conversation alias from the request body.
            response_id: Response id this alias should now resolve to.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO conversations (name, response_id) VALUES (?, ?)",
            (name, response_id),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection.

        Release must be idempotent and non-raising because it runs from
        ``APIServerAdapter.disconnect()``, which is invoked on the gateway
        shutdown and reconnect paths. An error here would otherwise mask the
        adapter teardown that follows it.
        """
        try:
            self._conn.close()
        except Exception:
            pass

    def __len__(self) -> int:
        """Return the number of stored responses (used by health reporting)."""
        row = self._conn.execute("SELECT COUNT(*) FROM responses").fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# CORS middleware
# ---------------------------------------------------------------------------
# Baseline CORS headers applied to every allowed origin. Only the two keys the
# browser actually needs to be told up front are listed here; the origin itself
# and the preflight cache lifetime are computed per request in
# ``APIServerAdapter._cors_headers_for_origin`` so a narrow allowlist stays
# narrow. ``Idempotency-Key`` is declared because the non-streaming
# /v1/chat/completions and /v1/responses paths cache retries on it.
_CORS_HEADERS = {
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Idempotency-Key",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def cors_middleware(request, handler):
        """Add CORS headers for explicitly allowed origins; handle OPTIONS preflight.

        Origin policy is fail-closed: a request carrying an ``Origin`` header
        that is not in the adapter's allowlist is rejected with 403 before any
        handler runs. Requests with no ``Origin`` at all (server-to-server
        clients, curl, the OpenAI SDK) are allowed through, which keeps the API
        usable from non-browser callers while still enforcing the allowlist for
        in-browser frontends that could otherwise read responses cross-origin.

        ``OPTIONS`` preflights never reach a route handler; they are answered
        here with 200 plus the CORS headers.
        """
        adapter = request.app.get("api_server_adapter")
        origin = request.headers.get("Origin", "")
        cors_headers = None
        if adapter is not None:
            if not adapter._origin_allowed(origin):
                return web.Response(status=403)
            cors_headers = adapter._cors_headers_for_origin(origin)

        if request.method == "OPTIONS":
            if cors_headers is None:
                return web.Response(status=403)
            return web.Response(status=200, headers=cors_headers)

        response = await handler(request)
        if cors_headers is not None:
            response.headers.update(cors_headers)
        return response
else:
    cors_middleware = None  # type: ignore[assignment]


# Extensions eligible for inline delivery. Anything else is left as a plain
# ``MEDIA:`` tag, which at least keeps the path visible to the caller.
_MEDIA_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# Explicit extension-to-content-type map so the emitted data URL always carries
# a correct MIME type; guessing from a caller-controlled suffix would let a
# renamed file claim an image type.
_MEDIA_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
# Five MB inline keeps a single base64 payload (about 6.7 MB encoded) small
# enough for typical HTTP buffers and browser limits; larger files stay as tags.
_MEDIA_DATA_URL_MAX_BYTES = 5 * 1024 * 1024  # skip images larger than 5MB


def _resolve_media_to_data_urls(text: str) -> str:
    """Replace ``MEDIA:<path>`` image tags with inline base64 data URLs.

    The agent expresses locally produced images as ``MEDIA:<absolute-path>``
    tags because IM platforms deliver files by path. Remote OpenAI-compatible
    frontends cannot read server paths, so a bare tag is useless to them; this
    helper turns small local images into markdown data URLs that render anywhere.

    Non-image, oversized, unreadable, or unsafe paths are left untouched rather
    than erroring, since the input is the agent's own reply text and a
    malformed tag should degrade to plain text instead of failing the response.

    Security
    --------
    Both gates come from :mod:`encre.gateway.platforms.base`, the same ones
    every other platform adapter's media delivery uses:

    * :data:`MEDIA_TAG_CLEANUP_RE` anchors on an absolute path with a known
      image suffix, so a relative or traversal token such as
      ``MEDIA:../../etc/passwd.png`` never matches.
    * :func:`validate_media_delivery_path` resolves the path and rejects it if
      it lands on the credential or system-path denylist.

    Reading an arbitrary local file just because its path appears in model
    output would exfiltrate it to the API caller, which is why the denylist and
    the absolute-path anchor are both mandatory here.

    Args:
        text: Final agent reply text, possibly containing ``MEDIA:`` tags.

    Returns:
        The same text with inlined images; unchanged when there is nothing to
        inline or the substitution fails.
    """
    if not text or "MEDIA:" not in text:
        return text
    import base64

    def _to_data_url(path_str: str) -> Optional[str]:
        """Build an inline data URL for one media path, or ``None`` to skip it.

        Every failure mode returns ``None`` rather than raising so the tag is
        left in place: unsafe paths, unknown extensions, files over the size cap,
        and unreadable files all degrade to the original text.

        Args:
            path_str: Path captured from a ``MEDIA:`` tag by the shared matcher.

        Returns:
            A markdown data URL, or ``None`` when the tag should stay as-is.
        """
        # validate_media_delivery_path() strips wrapping quotes/backticks
        # and trailing punctuation internally, same as MEDIA_TAG_CLEANUP_RE's
        # other callers (extract_media / _strip_media_tag_directives) rely on.
        # Stripping here would be redundant and could change the meaning of a
        # tag whose path legitimately ends in punctuation.
        safe_path = validate_media_delivery_path(path_str)
        if not safe_path:
            return None
        p = Path(safe_path)
        suffix = p.suffix.lower()
        if suffix not in _MEDIA_IMG_EXT:
            return None
        # Size is checked before reading so an oversized file is never loaded
        # into memory just to be discarded.
        try:
            if p.stat().st_size > _MEDIA_DATA_URL_MAX_BYTES:
                return None
            b64 = base64.b64encode(p.read_bytes()).decode()
        except OSError:
            return None
        return f"![image](data:{_MEDIA_MIME[suffix]};base64,{b64})"

    def _repl(m: "re.Match[str]") -> str:
        """Substitution callback for the media-tag regex.

        Falls back to the original match text when :func:`_to_data_url` declines
        so ``re.sub`` leaves the tag byte-identical instead of erasing it.
        """
        return _to_data_url(m.group("path")) or m.group(0)

    try:
        return MEDIA_TAG_CLEANUP_RE.sub(_repl, text)
    except Exception:
        return text


def _redact_api_error_text(value: Any, *, limit: int | None = None) -> str:
    """Redact API-bound error text before it crosses the HTTP boundary.

    Error strings built inside the agent can embed prompts, tool arguments,
    credential fragments, and stack traces. Anything returned in a response
    body, an SSE frame, an ``X-Encre-Error`` header, or a status record goes
    through here so that no internal secret reaches the wire.

    Args:
        value: Value to render as text; may not be a string.
        limit: Optional character cap for header-sized fields, which cannot
            safely carry long error text.

    Returns:
        Sanitized text, truncated to *limit* when given.
    """
    redacted = redact_sensitive_text(str(value), force=True)
    if limit is not None:
        return redacted[:limit]
    return redacted


def _openai_error(message: str, err_type: str = "invalid_request_error", param: str = None, code: str = None) -> Dict[str, Any]:
    """Build the OpenAI-style error envelope used by every error response.

    All error shapes in this module pass through here so SDK clients and
    frontends can parse failures uniformly: ``error.message`` carries the
    human-readable text (always redacted), ``error.type`` the category,
    ``error.param`` the offending field, and ``error.code`` a stable machine
    code handlers branch on.

    Args:
        message: Human-readable error text; redacted on the way in.
        err_type: Category, one of OpenAI's ``invalid_request_error``,
            ``authentication_error``, or ``server_error``.
        param: Field path that failed, or ``None``.
        code: Stable machine-readable code such as ``invalid_api_key``.

    Returns:
        The dict to pass to ``web.json_response``.

    Note:
        Handlers may attach an extra ``error["encre"]`` block with Encre-specific
        status fields after calling this; that is why the returned envelope is a
        plain dict rather than being handed to ``json_response`` here.
    """
    return {
        "error": {
            "message": _redact_api_error_text(message),
            "type": err_type,
            "param": param,
            "code": code,
        }
    }


# Shared reservation token for the in-flight API request owned by this context.
# Carries the mutable ``active``/``detached`` flags so a single slot can be
# released by exactly one of several owners: the decorator's ``finally``, the
# agent bookkeeping in :func:`APIServerAdapter._run_agent`, or a task's done
# callback after a handoff to background work.
_api_agent_request_reservation: ContextVar[Optional[dict[str, bool]]] = ContextVar(
    "api_agent_request_reservation", default=None
)


def _admit_api_agent_request(handler):
    """Reserve an authenticated API turn before its handler first awaits.

    Gateway shutdown and aiohttp requests share an event loop. Keeping the
    drain check and reservation in one non-awaiting block prevents a request
    admitted immediately before shutdown from becoming invisible while it is
    still parsing its body or resolving session state. The mutable reservation
    is intentionally shared with child tasks so agent/task bookkeeping releases
    this one slot exactly once.

    Order matters: authentication comes first so an unauthenticated caller can
    never influence the drain accounting, and the drain check comes before the
    reservation is taken so a draining gateway returns 503 instead of booking
    work it will never run.

    Args:
        handler: Bound agent-serving coroutine method to decorate. It receives
            ``(self, request, *args, **kwargs)`` unchanged.

    Returns:
        A wrapped coroutine with identical signature that owns the reservation.
    """
    @wraps(handler)
    async def _wrapped(self, request, *args, **kwargs):
        """Authenticate, refuse when draining, then run the handler.

        Returns the auth or drain error response directly when one applies.
        Otherwise the reservation slot is taken before the handler awaits and
        released exactly once, by whichever of the decorator or the agent's
        task bookkeeping still owns it.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        draining = self._draining_response()
        if draining is not None:
            return draining
        reservation = {"active": True}
        token = _api_agent_request_reservation.set(reservation)
        self._pending_agent_requests += 1
        try:
            return await handler(self, request, *args, **kwargs)
        finally:
            # Only release here if nobody transferred ownership. The slot is
            # clamped with max() because a background task may release after the
            # handler returns, and the counter must never go negative.
            if reservation["active"]:
                reservation["active"] = False
                self._pending_agent_requests = max(0, self._pending_agent_requests - 1)
            _api_agent_request_reservation.reset(token)

    return _wrapped


def _release_pending_api_work(adapter, reservation: dict[str, bool]) -> None:
    """Release a pending-work reservation exactly once.

    Idempotent by design: the ``active`` flag is cleared before decrementing so
    a task that races with the decorator's ``finally`` cannot double-decrement
    the counter. Called from task done-callbacks that outlive the request.

    Args:
        adapter: Owning adapter that holds the counter.
        reservation: The reservation dict created by the decorator or by
            :func:`_reserve_pending_api_work`.
    """
    if reservation["active"]:
        reservation["active"] = False
        adapter._pending_agent_requests = max(0, adapter._pending_agent_requests - 1)


@contextmanager
def _reserve_pending_api_work(adapter):
    """Keep externally-triggered background work visible across awaits.

    Used by entry points that start work triggered from outside a request, such
    as the Chronos fire webhook. A handler can detach the reservation to an
    asyncio task; its done callback then owns release so shutdown cannot miss
    the handoff to background work.

    Args:
        adapter: Owning adapter that holds the pending-work counter.

    Yields:
        The reservation dict; set ``reservation["detached"] = True`` when a
        task takes ownership so the ``finally`` block does not also release it.
    """
    reservation = {"active": True, "detached": False}
    adapter._pending_agent_requests += 1
    try:
        yield reservation
    finally:
        if not reservation["detached"]:
            _release_pending_api_work(adapter, reservation)


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def body_limit_middleware(request, handler):
        """Reject overly large request bodies early based on Content-Length.

        Two layers of defence: the ``Content-Length`` header is checked before
        the body is read at all, so an obviously oversized request never reaches
        a handler. Bodies without ``Content-Length`` (chunked transfer) are
        caught later by aiohttp's ``client_max_size``, which is rewrapped into
        the same 413 envelope so clients see one consistent error shape rather
        than a misleading 400 "Invalid JSON".
        """
        if request.method in {"POST", "PUT", "PATCH"}:
            cl = request.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > MAX_REQUEST_BYTES:
                        return web.json_response(_openai_error("Request body too large.", code="body_too_large"), status=413)
                except ValueError:
                    return web.json_response(_openai_error("Invalid Content-Length header.", code="invalid_content_length"), status=400)
        try:
            return await handler(request)
        except web.HTTPRequestEntityTooLarge:
            # aiohttp's client_max_size tripped mid-read (chunked bodies carry
            # no Content-Length) -- return a proper 413 instead of letting the
            # handler's broad JSON except turn it into 400 "Invalid JSON".
            return web.json_response(
                _openai_error("Request body too large.", code="body_too_large"),
                status=413,
            )
else:
    body_limit_middleware = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Request hardening: body size limits, security headers, idempotency
# ---------------------------------------------------------------------------
# Baseline response hardening applied to every response, including error
# responses and SSE streams. ``default-src 'none'`` and ``frame-ancestors
# 'none'`` keep this JSON API from ever rendering in a browser frame or loading
# resources; the remaining headers disable speculative sniffing and cross-site
# device access that a misconfigured reverse proxy could otherwise expose.
_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "0",
    "Referrer-Policy": "no-referrer",
}


if AIOHTTP_AVAILABLE:
    @web.middleware
    async def security_headers_middleware(request, handler):
        """Add security headers to all responses (including errors).

        Runs after the handler returns so that error responses produced inside
        a handler (401, 404, 429, ...) get the same headers as successes. Uses
        ``setdefault`` so a handler that deliberately overrides a header, such
        as an SSE stream setting its own ``Cache-Control``, keeps that value.
        """
        response = await handler(request)
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response
else:
    security_headers_middleware = None  # type: ignore[assignment]


class _IdempotencyCache:
    """In-memory idempotency cache with TTL and basic LRU semantics.

    Backs the ``Idempotency-Key`` header on the non-streaming
    ``/v1/chat/completions`` and ``/v1/responses`` paths. It serves two
    purposes at once:

    * Retry deduplication. Frontends and proxies retry failed HTTP calls, and
      the operation here is expensive and non-idempotent - it runs a full agent
      turn that may call tools with side effects. Replaying a cached response
      for the same key and body avoids charging the second attempt.
    * In-flight coalescing. Two requests with the same key start only one agent
      turn; the second awaits the same task rather than racing it.

    Invariants
    ----------
    * A stored entry is reused only when its fingerprint matches, so a reused
      key with a different body computes fresh rather than returning stale work.
    * Entries expire after ``ttl_seconds`` and the store never exceeds
      ``max_items``; both are enforced opportunistically on access.

    Thread safety
    -------------
    All access is synchronous and happens on the event loop, so the
    ``OrderedDict`` and the inflight map never see concurrent mutation. The
    cache is deliberately in-memory: persistence across restarts is not needed
    because a retried request after a restart is a fresh attempt.
    """
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 300):
        """Initialize the entry store, the inflight map, and both limits.

        Args:
            max_items: Maximum cached results before LRU eviction.
            ttl_seconds: Age, in seconds, after which an entry is dropped.
        """
        from collections import OrderedDict
        self._store = OrderedDict()
        self._inflight: Dict[tuple[str, str], "asyncio.Task[Any]"] = {}
        self._ttl = ttl_seconds
        self._max = max_items

    def _purge(self):
        """Drop expired entries, then evict oldest until within capacity.

        Eviction walks the ordering front-to-back because ``OrderedDict``
        records insertion order and nothing reorders on read here, so the
        front is the oldest entry.
        """
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v["ts"] > self._ttl]
        for k in expired:
            self._store.pop(k, None)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    async def get_or_set(self, key: str, fingerprint: str, compute_coro):
        """Return the cached result for *key*, computing it once if absent.

        Args:
            key: Client-supplied idempotency key.
            fingerprint: Hash of the request body fields that change meaning;
                a mismatch means the caller reused a key with a different
                request and gets a fresh computation.
            compute_coro: Zero-argument async callable that produces the
                ``(result, usage)`` tuple.

        Returns:
            The computed or cached ``(result, usage)`` tuple.

        Note:
            The awaiting task is shielded so a cancelled caller does not
            cancel the computation for everyone else awaiting the same task.
        """
        self._purge()
        item = self._store.get(key)
        if item and item["fp"] == fingerprint:
            return item["resp"]

        inflight_key = (key, fingerprint)
        task = self._inflight.get(inflight_key)
        if task is None:
            async def _compute_and_store():
                """Run the agent turn once and record the result on success."""
                # Compute, then record. Storing only on success means a failed
                # turn is retriable under the same key, which is what a client
                # that retries expects.
                resp = await compute_coro()
                import time as _t
                self._store[key] = {"resp": resp, "fp": fingerprint, "ts": _t.time()}
                self._purge()
                return resp

            task = asyncio.create_task(_compute_and_store())
            self._inflight[inflight_key] = task

            def _clear_inflight(done_task: "asyncio.Task[Any]") -> None:
                """Drop the inflight entry if this task still owns it."""
                # Only clear if this is still the recorded task, so a later
                # distinct computation for the same key is not removed early.
                if self._inflight.get(inflight_key) is done_task:
                    self._inflight.pop(inflight_key, None)

            task.add_done_callback(_clear_inflight)

        return await asyncio.shield(task)


# Single process-wide cache shared by both non-streaming agent endpoints.
_idem_cache = _IdempotencyCache()


def _make_request_fingerprint(body: Dict[str, Any], keys: List[str]) -> str:
    """Hash the request fields that define a request's meaning.

    Includes only the semantically meaningful keys for the given endpoint so
    that metadata-only differences (headers, session ids) do not defeat the
    idempotency cache. The result feeds :class:`_IdempotencyCache` as the
    second half of the inflight/store lookup key.

    Args:
        body: Parsed JSON request body.
        keys: Field names whose combination defines the computation.

    Returns:
        Hex SHA-256 digest of the selected subset.
    """
    from hashlib import sha256
    subset = {k: body.get(k) for k in keys}
    return sha256(repr(subset).encode("utf-8")).hexdigest()


def _derive_chat_session_id(
    system_prompt: Optional[str],
    first_user_message: str,
) -> str:
    """Derive a stable session ID from the conversation's first user message.

    OpenAI-compatible frontends (Open WebUI, LibreChat, etc.) send the full
    conversation history with every request.  The system prompt and first user
    message are constant across all turns of the same conversation, so hashing
    them produces a deterministic session ID that lets the API server reuse
    the same Encre session (and therefore the same Docker container sandbox
    directory) across turns.

    Args:
        system_prompt: Accumulated system text, or ``None`` for a bare chat.
        first_user_message: Content of the first user turn.

    Returns:
        A deterministic ``api-<16 hex chars>`` session id.

    Note:
        Truncating the digest to 16 hex chars keeps ids short while the
        collision space is still ~2^64, far above any realistic conversation
        count. The ``api-`` prefix distinguishes these ids from user-supplied
        ones passed via ``X-Encre-Session-Id``.
    """
    seed = f"{system_prompt or ''}\n{first_user_message}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"


# Optional cron integration. The cron modules live outside the packaged
# distribution, so _CRON_AVAILABLE is False and /api/jobs handlers degrade to
# 501 rather than crashing the whole adapter.
_CRON_AVAILABLE = False

_cron_list: callable = lambda *a, **kw: None  # type: ignore[assignment]
_cron_get: callable = lambda *a, **kw: None  # type: ignore[assignment]
_cron_create: callable = lambda *a, **kw: None  # type: ignore[assignment]
_cron_update: callable = lambda *a, **kw: None  # type: ignore[assignment]
_cron_remove: callable = lambda *a, **kw: None  # type: ignore[assignment]
_cron_pause: callable = lambda *a, **kw: None  # type: ignore[assignment]
_cron_resume: callable = lambda *a, **kw: None  # type: ignore[assignment]
_cron_trigger: callable = lambda *a, **kw: None  # type: ignore[assignment]


def _notify_cron_provider_jobs_changed() -> None:
    """Tell the active cron scheduler provider the job set changed after a REST
    mutation (no-op for the built-in). Best-effort - never breaks the handler.
    """
    pass

_scan_cron_prompt: callable = lambda *a, **kw: None  # type: ignore[assignment]


class APIServerAdapter(BasePlatformAdapter):
    """
    OpenAI-compatible HTTP API server adapter.

    Runs an aiohttp web server that accepts OpenAI-format requests
    and routes them through encre-agent's AIAgent.

    Position inside Encre
    ---------------------
    This adapter is the HTTP half of the gateway's platform layer, so it plugs
    into :class:`encre.gateway.run.GatewayRunner` exactly like Telegram or
    Discord. The desktop renderer and headless drivers still reach the same
    :class:`encre.server.session_manager.SessionManager` through the WebSocket
    transports, so all entry points converge on one agent core.

    Lifecycle
    ---------
    ``GatewayRunner`` constructs one instance per configured ``api_server``
    profile, calls :meth:`connect` to bind the listener, then calls :meth:`send`
    for nothing - delivery is request/response - and :meth:`disconnect` to tear
    down. ``connect`` may be retried by the reconnect watcher, which is why
    every owned resource is released in ``disconnect``.

    Request path
    ------------
    Middleware (profile prefix, CORS, body limit, security headers) runs first,
    then the route handler authenticates via :meth:`_check_auth`, and agent-
    serving handlers additionally pass through :func:`_admit_api_agent_request`
    to honor shutdown draining and the concurrency cap. The agent itself is
    built by :func:`_create_agent` and executed in a thread executor by
    :func:`_run_agent`.

    Attributes
    ----------
    _host, _port, _api_key:
        Bind address and bearer key. The key is mandatory;
        :func:`_api_key_passes_startup_guard` refuses to start without a
        strong one.
    _cors_origins:
        Tuple of allowed browser origins.
    _model_name:
        Advertised model id for ``GET /v1/models``.
    _model_routes:
        Client-supplied ``model`` aliases mapped to provider/model overrides.
    _response_store:
        :class:`ResponseStore` backing the stateful Responses API.
    _run_streams, _run_streams_created, _run_stream_subscribers:
        Transport state for the ``/v1/runs`` SSE feed. These are deliberately
        separate from run status so a dead subscriber does not end a run.
    _active_run_agents, _active_run_tasks, _stopping_run_ids:
        Control-plane state used by ``/v1/runs/{run_id}/stop``.
    _run_statuses, _run_approval_sessions:
        Pollable status and the approval session key for each live run.
    gateway_runner:
        Back-reference set by ``GatewayRunner`` so sibling platform adapters can
        be resolved for the platform event callback ingress.
    _background_tasks:
        Strong references to fire-and-forget tasks, inherited from
        :class:`encre.gateway.platforms.base.BasePlatformAdapter`, preventing
        task collection mid-flight.
    """

    # Stateless request/response: every route (the OpenAI-spec
    # /v1/chat/completions and /v1/responses, and the proprietary /v1/runs SSE
    # stream) tears down its channel when the turn ends. There is no persistent
    # outbound channel to push a background completion to a client that already
    # received its response, and ``send()`` is a no-op stub. So async-delivery
    # tools (terminal notify_on_complete / watch_patterns, delegate_task
    # background=True) must NOT promise delivery on this path - see
    # ``async_delivery_supported()``.
    supports_async_delivery: bool = False

    # Same statelessness applies to the startup auto-resume prompt: no client
    # is waiting to answer "session restored - what next?", so a resumed turn
    # should complete the interrupted work rather than acknowledge and wait
    # for a follow-up that will never arrive on this interface.
    interactive_resume: bool = False

    def __init__(self, config: PlatformConfig):
        """Configure the adapter and pre-allocate its transport state.

        Every tunable comes from ``config.extra`` first, then the corresponding
        ``API_SERVER_*`` environment variable, then a module default. Config
        wins because it is the version-controlled deployment source; env vars
        exist so a container can override without editing ``config.yaml``.

        Nothing network-related happens here: the listener is only bound in
        :meth:`connect`. Building the :class:`ResponseStore` does touch the
        filesystem, which is acceptable because it fails soft to an in-memory
        database.

        Args:
            config: The ``api_server`` platform config block, including the
                ``extra`` mapping holding host, port, key, CORS origins, model
                name, and ``model_routes``.
        """
        super().__init__(config, Platform.API_SERVER)
        extra = config.extra or {}
        self._host: str = extra.get("host", os.getenv("API_SERVER_HOST", DEFAULT_HOST))
        raw_port = extra.get("port")
        if raw_port is None:
            raw_port = os.getenv("API_SERVER_PORT", str(DEFAULT_PORT))
        self._port: int = _coerce_port(raw_port, DEFAULT_PORT)
        self._api_key: str = extra.get("key", os.getenv("API_SERVER_KEY", ""))
        self._cors_origins: tuple[str, ...] = self._parse_cors_origins(
            extra.get("cors_origins", os.getenv("API_SERVER_CORS_ORIGINS", "")),
        )
        self._model_name: str = self._resolve_model_name(
            extra.get("model_name", os.getenv("API_SERVER_MODEL_NAME", "")),
        )
        # model_routes: maps incoming ``model`` field values to specific
        # provider/model configs so one API server instance can serve
        # multiple clients on different backends.
        #
        # Config format (platforms.api_server.extra in the gateway config):
        #   model_routes:
        #     minimax-m2:          # alias the client sends as the "model" field
        #       model: "minimax/minimax-m1"
        #       provider: "openrouter"   # optional - resolved via the provider
        #                                # credential chain when set
        #       api_key: "sk-..."         # optional - per-route UPSTREAM provider
        #                                # key override (NOT caller auth; never logged)
        #       base_url: "https://..."   # optional - per-route base URL override
        self._model_routes: Dict[str, Dict[str, Any]] = self._parse_model_routes(
            extra.get("model_routes"),
        )
        # aiohttp objects are created in connect(); kept as instance state so
        # disconnect() can tear down exactly what connect() built and so a
        # failed start leaves no half-initialised listener behind.
        self._app: Optional["web.Application"] = None
        self._runner: Optional["web.AppRunner"] = None
        self._site: Optional["web.TCPSite"] = None
        self._response_store = ResponseStore()
        # Active run streams: run_id -> asyncio.Queue of SSE event dicts.
        # This is transport state only: a queue here means "someone may be
        # reading this run", not "the run is alive".
        self._run_streams: Dict[str, "asyncio.Queue[Optional[Dict]]"] = {}
        # Creation timestamps for orphaned-run TTL sweep
        self._run_streams_created: Dict[str, float] = {}
        # Runs with a connected SSE consumer; their queue is actively draining.
        # A subscriber is the difference between "drop the queue when it ages
        # out" and "keep buffering for a client that is still attached".
        self._run_stream_subscribers: set[str] = set()
        # Active run agent/task references for stop support
        self._active_run_agents: Dict[str, Any] = {}
        self._active_run_tasks: Dict[str, "asyncio.Task"] = {}
        # Stop is cooperative: the executor thread may outlive the HTTP request.
        # Marking here is all /stop can do synchronously; the executor notices
        # the flag at its next loop iteration.
        self._stopping_run_ids: set[str] = set()
        # Pollable run status for dashboards and external control-plane UIs.
        # Retained past the transport queue so a control-plane poller that
        # never connected the SSE feed still sees the outcome.
        self._run_statuses: Dict[str, Dict[str, Any]] = {}
        # Active approval session key for each run_id.  The approval core
        # resolves requests by session key, while API clients address the
        # in-flight run by run_id, so this map is the bridge between the two
        # naming schemes.
        self._run_approval_sessions: Dict[str, str] = {}
        self._session_db: Optional[Any] = None  # Lazy-init SessionDB for session continuity
        self._session_db_lock: Optional[asyncio.Lock] = None  # Single-flight for lazy init
        # Concurrency cap shared across all agent-serving endpoints
        # (/v1/chat/completions, /v1/responses, /v1/runs). Read from
        # config.yaml encre.gateway.api_server.max_concurrent_runs; 0 disables
        # the cap. Bounds CPU / memory / upstream-LLM-quota exhaustion
        # from a request flood.
        self._max_concurrent_runs: int = self._resolve_max_concurrent_runs()
        # Number of in-flight runs on the non-streaming chat/responses paths
        # (the /v1/runs path tracks its own in-flight set via
        # _active_run_tasks). Incremented in _run_agent, decremented in its
        # finally block, so it tracks agent execution rather than HTTP work.
        self._inflight_agent_runs: int = 0
        # Back-reference to the owning GatewayRunner (set by gateway/run.py)
        # so /api/platforms/{platform}/events can resolve sibling adapters.
        # BasePlatformAdapter declares the class-level default of None.
        self.gateway_runner: Optional[Any] = None
        # Requests admitted before their handler reaches agent bookkeeping.
        # Shutdown counts this reservation so the request cannot slip through
        # the drain between its first await and _run_agent()/task registration.
        self._pending_agent_requests: int = 0

    def active_agent_work_count(self) -> int:
        """Return all live agent work owned by this API adapter.

        ``/v1/runs`` registers an asyncio task before it constructs and stores
        its agent, so ``_active_run_agents`` has a real queued-before-agent gap.
        Reuse the task-based accounting used by the concurrent-run limit: it
        covers that gap and excludes completed tasks retained until cleanup.
        """
        try:
            return (
                int(getattr(self, "_pending_agent_requests", 0))
                + int(self._inflight_agent_runs)
                + sum(not task.done() for task in self._active_run_tasks.values())
            )
        except Exception:
            return 0

    @staticmethod
    def _gateway_is_draining() -> bool:
        """Whether the owning gateway currently refuses new agent turns."""
        try:
            from encre.gateway.run import _gateway_runner_ref

            runner = _gateway_runner_ref()
            return bool(
                runner
                and (
                    getattr(runner, "_draining", False)
                    or getattr(runner, "_external_drain_active", False)
                )
            )
        except Exception:
            return False

    def _draining_response(self) -> Optional["web.Response"]:
        """Return a retryable response while the gateway drains existing work.

        Called by :func:`_admit_api_agent_request` immediately before a request
        is admitted. A 503 with ``Retry-After`` is used rather than a 500 or a
        connection reset because a drain is transient and well-behaved clients
        (and load balancers) know how to retry it.

        Returns:
            ``None`` when the gateway is accepting work, otherwise the 503
            response that callers should return verbatim.
        """
        if not self._gateway_is_draining():
            return None
        return web.json_response(
            _openai_error(
                "Gateway is draining existing work; retry shortly.",
                code="gateway_draining",
            ),
            status=503,
            headers={"Retry-After": "1"},
        )

    def _activate_admitted_request(self) -> None:
        """Transfer this request's drain reservation to agent bookkeeping.

        Called at the point a request is about to start real agent work, so
        the pending-request reservation is swapped for the in-flight-run
        accounting owned by :func:`_run_agent` or the ``/v1/runs`` task
        registry. This is what makes the admission decision non-racy across the
        awaits between the drain check and agent registration.

        Idempotent: if the reservation was already released, nothing happens.
        """
        reservation = _api_agent_request_reservation.get()
        if reservation and reservation["active"]:
            reservation["active"] = False
            self._pending_agent_requests = max(0, self._pending_agent_requests - 1)

    def _readiness_work_counts(self) -> tuple[int, int, int]:
        """Return bounded work counts from each subsystem's public state.

        Feed for :func:`encre.gateway.readiness.collect_runtime_readiness` in
        ``GET /health/detailed``. Each count is read defensively because these
        tool registries are optional imports; an unavailable subsystem reports
        0 rather than turning the health endpoint into a 500.

        Returns:
            ``(active_api_runs, process_completion_queue_depth,
            active_delegations)``.
        """
        active_api_runs = sum(
            1
            for status in self._run_statuses.values()
            if status.get("status") in {"queued", "running", "waiting_for_approval"}
        )
        process_depth = 0
        active_delegations = 0
        try:
            from tools.process_registry import process_registry

            process_depth = process_registry.completion_queue.qsize()
        except Exception:
            pass
        try:
            from tools.async_delegation import active_count

            active_delegations = active_count()
        except Exception:
            pass
        return active_api_runs, process_depth, active_delegations

    @staticmethod
    def _parse_cors_origins(value: Any) -> tuple[str, ...]:
        """Normalize configured CORS origins into a stable tuple.

        Accepts a comma-separated string (the env-var convention) or a list,
        tuple, or set (the ``config.yaml`` convention) and returns one canonical
        tuple either way. Blank entries are dropped and every entry is stripped,
        so a stray space in the allowlist does not silently disable a rule.

        Args:
            value: Raw ``cors_origins`` from config or
                ``API_SERVER_CORS_ORIGINS``.

        Returns:
            The cleaned allowlist, or an empty tuple when unset.
        """
        if not value:
            return ()

        if isinstance(value, str):
            items = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            items = value
        else:
            items = [str(value)]

        return tuple(str(item).strip() for item in items if str(item).strip())

    @staticmethod
    def _resolve_max_concurrent_runs() -> int:
        """Read the concurrent-run cap from config.yaml (0 disables).

        Path: ``encre.gateway.api_server.max_concurrent_runs``. Falls back to
        the historical default of 10 when unset or malformed. Negative values
        are clamped to 0 (disabled).

        A misconfiguration must not prevent the API server from starting, so
        every failure mode returns the default rather than raising.
        """
        default = 10
        try:
            from encre.config import cfg_get, load_config

            raw = cfg_get(
                load_config(),
                "gateway",
                "api_server",
                "max_concurrent_runs",
                default=default,
            )
            value = int(raw)
        except Exception:
            return default
        return max(0, value)

    @staticmethod
    def _resolve_model_name(explicit: str) -> str:
        """Derive the advertised model name for /v1/models.

        Priority:
        1. Explicit override (config extra or API_SERVER_MODEL_NAME env var)
        2. Active profile name (so each profile advertises a distinct model)
        3. Fallback: "encre-agent"

        Deriving from the profile matters under multiplex: two profiles on the
        same listener must not both claim ``encre-agent`` or a client listing
        models cannot tell them apart. Generic profile names are skipped so the
        fallback stays human-readable.

        Args:
            explicit: Value supplied in config or via the environment.

        Returns:
            The model id to advertise.
        """
        if explicit and explicit.strip():
            return explicit.strip()
        try:
            from encre.profiles import get_active_profile_name
            profile = get_active_profile_name()
            if profile and profile not in {"default", "custom"}:
                return profile
        except Exception:
            pass
        return "encre-agent"

    def _cors_headers_for_origin(self, origin: str) -> Optional[Dict[str, str]]:
        """Return CORS headers for an allowed browser origin.

        Computes headers rather than reusing a static dict because the origin
        itself must be reflected and because a wildcard allowlist and a specific
        one need different ``Vary`` handling: with a specific origin the response
        varies by origin and must say so, or a shared cache could serve one
        origin's credentials-bearing response to another.

        Args:
            origin: The ``Origin`` header value.

        Returns:
            Headers to apply, or ``None`` when no CORS headers apply (no origin
            set, no allowlist, or origin not allowed).
        """
        if not origin or not self._cors_origins:
            return None

        if "*" in self._cors_origins:
            headers = dict(_CORS_HEADERS)
            headers["Access-Control-Allow-Origin"] = "*"
            headers["Access-Control-Max-Age"] = "600"
            return headers

        if origin not in self._cors_origins:
            return None

        headers = dict(_CORS_HEADERS)
        headers["Access-Control-Allow-Origin"] = origin
        headers["Vary"] = "Origin"
        headers["Access-Control-Max-Age"] = "600"
        return headers

    def _origin_allowed(self, origin: str) -> bool:
        """Allow non-browser clients and explicitly configured browser origins."""
        if not origin:
            return True

        if not self._cors_origins:
            return False

        return "*" in self._cors_origins or origin in self._cors_origins

    @staticmethod
    def _clean_log_value(value: Any, *, max_len: int = 200) -> str:
        """Sanitize request metadata before it reaches security logs."""
        if value is None:
            return ""
        text = str(value).replace("\r", " ").replace("\n", " ").strip()
        return text[:max_len]

    def _request_audit_context(self, request: "web.Request") -> Dict[str, str]:
        """Return non-secret source metadata for security/audit warnings."""
        peer_ip = ""
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
            if isinstance(peer, (tuple, list)) and peer:
                peer_ip = str(peer[0])
        except Exception:
            peer_ip = ""

        return {
            "remote": self._clean_log_value(getattr(request, "remote", "") or peer_ip),
            "peer_ip": self._clean_log_value(peer_ip),
            "forwarded_for": self._clean_log_value(request.headers.get("X-Forwarded-For", "")),
            "real_ip": self._clean_log_value(request.headers.get("X-Real-IP", "")),
            "method": self._clean_log_value(request.method, max_len=16),
            "path": self._clean_log_value(request.path_qs, max_len=500),
            "user_agent": self._clean_log_value(request.headers.get("User-Agent", ""), max_len=300),
        }

    def _request_audit_log_suffix(self, request: "web.Request") -> str:
        """Render the audit context as a ``key='value'`` log suffix.

        Joins only non-empty fields so a log line never carries a run of
        ``key=''`` noise, and falls back to ``source='unknown'`` when nothing
        was extractable so the warning is still attributable to a request.

        Args:
            request: The aiohttp request being authenticated.

        Returns:
            A single string suitable for appending to a log message.
        """
        ctx = self._request_audit_context(request)
        fields = [f"{key}={value!r}" for key, value in ctx.items() if value]
        return " ".join(fields) if fields else "source='unknown'"

    def _cron_origin_from_request(self, request: "web.Request") -> Dict[str, str]:
        """Persist safe API source metadata on cron jobs created over HTTP."""
        ctx = self._request_audit_context(request)
        origin = {
            "platform": "api_server",
            "chat_id": "api",
        }
        if ctx.get("remote"):
            origin["source_ip"] = ctx["remote"]
        if ctx.get("peer_ip"):
            origin["peer_ip"] = ctx["peer_ip"]
        if ctx.get("forwarded_for"):
            origin["forwarded_for"] = ctx["forwarded_for"]
        if ctx.get("real_ip"):
            origin["real_ip"] = ctx["real_ip"]
        if ctx.get("user_agent"):
            origin["user_agent"] = ctx["user_agent"]
        return origin

    # ------------------------------------------------------------------
    # Auth helper
    # ------------------------------------------------------------------

    def _check_auth(self, request: "web.Request") -> Optional["web.Response"]:
        """
        Validate Bearer token from Authorization header.

        Returns None if auth is OK, or a 401 web.Response on failure.
        connect() refuses to start the API server without API_SERVER_KEY, so
        the no-key branch only exists for tests or unsupported manual wiring.
        """
        if not self._api_key:
            return None

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            # Compare as bytes: ``hmac.compare_digest`` raises TypeError on a
            # str containing non-ASCII characters, and ``token`` is the raw
            # client-supplied header. A stray non-ASCII byte in the key would
            # otherwise crash this handler (500) instead of returning a clean
            # 401. Encoding both sides keeps the timing-safe comparison and
            # matches web_server.py's dashboard-token check.
            if hmac.compare_digest(token.encode(), self._api_key.encode()):
                return None  # Auth OK

        logger.warning(
            "API server rejected invalid API key: %s",
            self._request_audit_log_suffix(request),
        )
        return web.json_response(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}},
            status=401,
        )

    @staticmethod
    def _normalize_callback_platform(value: str) -> str:
        """Normalize a callback platform name to ``[a-z0-9_]+`` or ``""``.

        The value comes straight from a URL path segment, so it is lowercased
        and hyphens are folded before being validated against a strict
        identifier pattern. Anything else is rejected by returning ``""``,
        which callers treat as an invalid-platform 400 rather than letting an
        arbitrary string index into the adapter registry.

        Args:
            value: Raw platform segment from the URL.

        Returns:
            The normalized name, or ``""`` when it is not usable.
        """
        normalized = (value or "").strip().lower().replace("-", "_")
        if not re.fullmatch(r"[a-z0-9_]+", normalized):
            return ""
        return normalized

    def _get_platform_callback_adapter(
        self,
        request: "web.Request",
        platform_name: str,
    ) -> Optional[Any]:
        """Resolve the connected adapter for *platform_name*, or ``None``.

        Probes three sources in order of specificity: the injected adapter
        map, the flat ``<name>_adapter`` app key, and the gateway runner's own
        adapter table. The last source is wrapped because ``Platform`` may be
        unavailable when this runs outside a full gateway, in which case the
        adapters are matched by value instead.

        Args:
            request: The aiohttp request, used to reach application state.
            platform_name: Normalized platform identifier.

        Returns:
            The adapter instance, or ``None`` if none is connected.
        """
        injected = request.app.get("platform_event_adapters")
        if isinstance(injected, dict):
            adapter = injected.get(platform_name)
            if adapter is not None:
                return adapter

        adapter = request.app.get(f"{platform_name}_adapter")
        if adapter is not None:
            return adapter

        runner = self.gateway_runner or request.app.get("gateway_runner")
        adapters = getattr(runner, "adapters", None)
        if not adapters:
            return None

        try:
            from encre.gateway.config import Platform as _Platform
            return adapters.get(_Platform(platform_name))
        except Exception:
            for platform, candidate in adapters.items():
                if getattr(platform, "value", platform) == platform_name:
                    return candidate
        return None

    async def _handle_platform_event_callback(self, request: "web.Request") -> "web.Response":
        """Verify and dispatch an inbound platform webhook event.

        The adapter's own verifier authenticates the request before the payload
        is even parsed, and a verifier that raises is treated as a rejection
        rather than an admission. Sync verifiers are run in a worker thread
        because some perform blocking network I/O.

        Returns:
            The adapter's dispatch result as JSON, or an OpenAI-shaped error
            response: 400 for an invalid platform name or body, 401 for a
            failed verifier, 503 when no adapter is connected, 500 for a
            dispatch failure.
        """
        platform_name = self._normalize_callback_platform(
            request.match_info.get("platform", "")
        )
        if not platform_name:
            return web.json_response(
                _openai_error(
                    "Invalid platform name",
                    code="invalid_platform",
                ),
                status=400,
            )

        adapter = self._get_platform_callback_adapter(request, platform_name)
        if adapter is None:
            return web.json_response(
                _openai_error(
                    "Platform adapter is not connected",
                    code="platform_unavailable",
                ),
                status=503,
            )

        verifier = getattr(adapter, "verify_http_event_request", None)
        dispatcher = getattr(adapter, "dispatch_http_event", None)
        if verifier is None or dispatcher is None:
            return web.json_response(
                _openai_error(
                    "Platform adapter does not support HTTP events",
                    code="platform_http_events_unsupported",
                ),
                status=503,
            )

        auth_header = request.headers.get("Authorization", "")
        try:
            if asyncio.iscoroutinefunction(verifier):
                ok, code = await verifier(auth_header)
            else:
                # Platform verifiers may do blocking network I/O (e.g. Google
                # signing-cert fetches) -- keep that off the event loop.
                ok, code = await asyncio.to_thread(verifier, auth_header)
        except Exception:
            # Fail closed: a crashing verifier must never admit the event.
            logger.exception(
                "Platform HTTP event verifier failed for %s", platform_name
            )
            ok, code = False, "platform_event_verifier_error"
        if not ok:
            return web.json_response(
                _openai_error(
                    "Invalid platform event authorization",
                    code=code or "invalid_platform_event_authorization",
                ),
                status=401,
            )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                _openai_error("Invalid JSON in platform event", code="invalid_json"),
                status=400,
            )

        if not isinstance(payload, dict):
            return web.json_response(
                _openai_error(
                    "Platform event must be a JSON object",
                    code="invalid_request",
                ),
                status=400,
            )

        try:
            result = await dispatcher(payload)
        except Exception:
            logger.exception("Platform HTTP event dispatch failed for %s", platform_name)
            return web.json_response(
                _openai_error(
                    "Platform event dispatch failed",
                    err_type="server_error",
                    code="platform_event_dispatch_failed",
                ),
                status=500,
            )

        return web.json_response(result if isinstance(result, dict) else {})

    # ------------------------------------------------------------------
    # Multi-profile multiplexing (/p/<profile>/--
    # ------------------------------------------------------------------

    def _resolve_request_profile(self, request: "web.Request"):
        """Resolve + validate the /p/<profile>/ URL prefix on an API request.

        Returns:
          - ``None`` when no profile prefix is present, or multiplexing is off
            (the prefix is ignored; request handled as the default profile).
          - the profile name (str) when present, multiplexing is on, and the
            profile is one this gateway serves.
          - ``_PROFILE_REJECTED`` when a prefix is present but the profile is
            unknown/unconfigured (handler/middleware returns 404).
        """
        profile = (request.match_info.get("profile") or "").strip()
        if not profile:
            return None
        runner = getattr(self, "gateway_runner", None)
        cfg = getattr(runner, "config", None)
        if not getattr(cfg, "multiplex_profiles", False):
            # Prefix supplied but multiplexing is off -- ignore it, behave as
            # the single-profile gateway (don't 404 a would-be valid route).
            return None
        try:
            from encre.profiles import profiles_to_serve

            served = {name for name, _ in profiles_to_serve(multiplex=True)}
        except Exception:
            return _PROFILE_REJECTED
        if profile not in served:
            return _PROFILE_REJECTED
        return profile

    @staticmethod
    def _profile_scope(profile: Optional[str]):
        """Enter the multiplex profile runtime scope, or a no-op when unset.

        When no ``/p/<profile>/`` prefix was given AND multiplexing is active,
        enter the DEFAULT profile's scope instead of a no-op: api_server is a
        port-binding platform that lives on the default profile, and with
        multiplex fail-closed ``get_secret`` active, an unscoped agent run
        raises ``UnscopedSecretError`` on its first credential read (#61276).
        Single-profile gateways keep the no-op --``get_secret`` falls through
        to ``os.environ`` there, unchanged.
        """
        if not profile:
            try:
                from agent.secret_scope import is_multiplex_active

                if is_multiplex_active():
                    from encre.gateway.run import _profile_runtime_scope
                    from encre.config import get_data_dir

                    return _profile_runtime_scope(get_data_dir())
            except Exception:
                pass
            return nullcontext()
        from encre.gateway.run import _profile_runtime_scope
        from encre.profiles import get_profile_dir

        return _profile_runtime_scope(get_profile_dir(profile))

    def _make_profile_prefix_middleware(self):
        """Reject unknown /p/<profile>/ prefixes and scope the request home."""

        @web.middleware
        async def profile_prefix_middleware(request: "web.Request", handler):
            """Reject unknown profiles, then scope the request's profile home.

            Sets the request-profile context var and enters the profile runtime
            scope for the duration of the handler, so any downstream secret or
            data-dir lookup resolves to that profile's directory. Both are
            released in ``finally`` so a rejected request cannot leak scope.
            """
            profile = self._resolve_request_profile(request)
            if profile is _PROFILE_REJECTED:
                return web.json_response(
                    {"error": "Unknown or unconfigured profile"},
                    status=404,
                )
            token = _api_request_profile.set(profile)
            try:
                with self._profile_scope(profile):
                    return await handler(request)
            finally:
                _api_request_profile.reset(token)

        return profile_prefix_middleware

    def _http_route_table(self) -> List[tuple]:
        """Return (method, path, handler) rows registered by ``connect()``.

        Kept as a method so multiplex tests can assert the /p/<profile>/
        mirrors without starting a real aiohttp listener.
        """
        routes: List[tuple] = [
            ("GET", "/health", self._handle_health),
            ("GET", "/health/detailed", self._handle_health_detailed),
            ("GET", "/v1/health", self._handle_health),
            ("GET", "/v1/models", self._handle_models),
            ("GET", "/v1/capabilities", self._handle_capabilities),
            ("GET", "/v1/skills", self._handle_skills),
            ("GET", "/v1/toolsets", self._handle_toolsets),
            ("GET", "/api/sessions", self._handle_list_sessions),
            ("POST", "/api/sessions", self._handle_create_session),
            ("GET", "/api/sessions/{session_id}", self._handle_get_session),
            ("PATCH", "/api/sessions/{session_id}", self._handle_patch_session),
            ("DELETE", "/api/sessions/{session_id}", self._handle_delete_session),
            ("GET", "/api/sessions/{session_id}/messages", self._handle_session_messages),
            ("POST", "/api/sessions/{session_id}/fork", self._handle_fork_session),
            ("POST", "/api/sessions/{session_id}/chat", self._handle_session_chat),
            ("POST", "/api/sessions/{session_id}/chat/stream", self._handle_session_chat_stream),
            ("POST", "/v1/chat/completions", self._handle_chat_completions),
            ("POST", "/v1/responses", self._handle_responses),
            ("GET", "/v1/responses/{response_id}", self._handle_get_response),
            ("DELETE", "/v1/responses/{response_id}", self._handle_delete_response),
            # Generic platform HTTP event callback ingress. Authenticated by
            # the target adapter's own verifier (platform-signed bearer), NOT
            # API_SERVER_KEY -- external platforms hold no API server key.
            ("POST", "/api/platforms/{platform}/events", self._handle_platform_event_callback),
            ("GET", "/api/jobs", self._handle_list_jobs),
            ("POST", "/api/jobs", self._handle_create_job),
            ("GET", "/api/jobs/{job_id}", self._handle_get_job),
            ("PATCH", "/api/jobs/{job_id}", self._handle_update_job),
            ("DELETE", "/api/jobs/{job_id}", self._handle_delete_job),
            ("POST", "/api/jobs/{job_id}/pause", self._handle_pause_job),
            ("POST", "/api/jobs/{job_id}/resume", self._handle_resume_job),
            ("POST", "/api/jobs/{job_id}/run", self._handle_run_job),
            ("POST", "/v1/runs", self._handle_runs),
            ("GET", "/v1/runs/{run_id}", self._handle_get_run),
            ("GET", "/v1/runs/{run_id}/events", self._handle_run_events),
            ("POST", "/v1/runs/{run_id}/approval", self._handle_run_approval),
            ("POST", "/v1/runs/{run_id}/stop", self._handle_stop_run),
        ]
        if _CRON_AVAILABLE:
            # Chronos managed-cron fire webhook (NAS -> agent). Authenticated
            # by a NAS-minted JWT (NOT API_SERVER_KEY).
            routes.append(("POST", "/api/cron/fire", self._handle_cron_fire))
        return routes

    # ------------------------------------------------------------------
    # Session header helpers
    # ------------------------------------------------------------------

    # Soft length cap for session identifiers.  Headers are bounded in
    # aggregate by aiohttp (``client_max_size`` / default 8 KiB per
    # header), but we impose a tighter limit on the session headers so a
    # caller can't burn memory by passing a multi-kilobyte "session key".
    # 256 chars is well above any realistic stable channel identifier
    # (e.g. ``agent:main:webui:dm:user-42``) while staying small enough
    # that the sanitized form is safe to pass into Honcho / state.db.
    _MAX_SESSION_HEADER_LEN = 256

    def _parse_session_key_header(
        self, request: "web.Request"
    ) -> tuple[Optional[str], Optional["web.Response"]]:
        """Extract and validate the ``X-Encre-Session-Key`` header.

        The session key is a stable per-channel identifier that scopes
        long-term memory (e.g. Honcho sessions) across transcripts.  It
        is independent of ``X-Encre-Session-Id``: callers may send
        either, both, or neither.

        Returns ``(session_key, None)`` on success (with an empty/absent
        header yielding ``None`` for the key), or ``(None, error_response)``
        on validation failure.

        Security: like session continuation, accepting a caller-supplied
        memory scope requires API-key authentication so that an
        unauthenticated client on a local-only server can't inject itself
        into another user's long-term memory scope by guessing a key.
        """
        raw = request.headers.get("X-Encre-Session-Key", "").strip()
        if not raw:
            return None, None

        if not self._api_key:
            logger.warning(
                "X-Encre-Session-Key rejected: no API key configured. "
                "Set API_SERVER_KEY to enable long-term memory scoping."
            )
            return None, web.json_response(
                _openai_error(
                    "X-Encre-Session-Key requires API key authentication. "
                    "Configure API_SERVER_KEY to enable this feature."
                ),
                status=403,
            )

        # Reject control characters that could enable header injection on
        # the echo path.
        if re.search(r'[\r\n\x00]', raw):
            return None, web.json_response(
                {"error": {"message": "Invalid session key", "type": "invalid_request_error"}},
                status=400,
            )

        if len(raw) > self._MAX_SESSION_HEADER_LEN:
            return None, web.json_response(
                {"error": {"message": "Session key too long", "type": "invalid_request_error"}},
                status=400,
            )

        return raw, None

    # ------------------------------------------------------------------
    # Session DB helper
    # ------------------------------------------------------------------

    def _open_and_cache_session_db(self, home) -> Optional[Any]:
        """Sync core: return the cached SessionDB for ``home``, opening it once.

        Shared by the sync (``_ensure_session_db``) and async
        (``_ensure_session_db_async``) entry points so both honor the same
        per-profile cache. Deliberately does NOT write into ``self._session_db``
        -- that stays reserved for an explicit test/manual override, so the first
        profile served can't pin every later request to its DB.
        """
        from encre.state import SessionDB

        key = str(home)
        cache = getattr(self, "_session_dbs", None)
        if cache is None:
            cache = {}
            self._session_dbs = cache
        db = cache.get(key)
        if db is None:
            db = SessionDB(db_path=home / "state.db")
            cache[key] = db
        return db

    def _ensure_session_db(self):
        """Lazily initialise and return the SessionDB for the active profile home.

        Sessions are persisted to ``state.db`` so that ``encre sessions list``
        shows API-server conversations alongside CLI and gateway ones.

        Under multiplex ``/p/<profile>/`` requests the profile runtime scope
        redirects ``get_data_dir()``, so each profile gets its own DB --
        never the default profile's file. Synchronous: used by ``_create_agent``
        (itself sync, and run in both loop and worker contexts). Request
        handlers use ``_ensure_session_db_async`` to keep the SQLite open off
        the event loop.
        """
        # Explicit override (tests / manual wiring) wins.
        if self._session_db is not None:
            return self._session_db
        try:
            from encre.config import get_data_dir

            return self._open_and_cache_session_db(get_data_dir())
        except Exception as e:
            logger.debug("SessionDB unavailable for API server: %s", e)
            return None

    async def _ensure_session_db_async(self):
        """Async variant for request handlers: offload the SQLite open/schema
        init off the single aiohttp event-loop thread.

        The active profile home is captured on the loop thread (its runtime
        scope is not visible inside ``asyncio.to_thread``); only the blocking
        construction runs in the worker. A single-flight lock prevents duplicate
        concurrent construction for the same home.
        """
        if self._session_db is not None:
            return self._session_db
        try:
            from encre.config import get_data_dir

            home = get_data_dir()
            key = str(home)
            cache = getattr(self, "_session_dbs", None)
            if cache is not None and cache.get(key) is not None:
                return cache[key]
            if self._session_db_lock is None:
                self._session_db_lock = asyncio.Lock()
            async with self._session_db_lock:
                cache = getattr(self, "_session_dbs", None)
                if cache is not None and cache.get(key) is not None:
                    return cache[key]
                return await asyncio.to_thread(self._open_and_cache_session_db, home)
        except Exception as e:
            logger.debug("SessionDB unavailable for API server: %s", e)
            return None

    # ------------------------------------------------------------------
    # Agent creation helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_model_routes(raw: Any) -> Dict[str, Dict[str, Any]]:
        """Validate and normalize the ``model_routes`` config block.

        Accepts a mapping of ``alias -> {model, provider?, api_key?, base_url?}``.
        Invalid shapes are dropped (never raised) so a config typo can't take
        the whole API server down.  Route values are coerced to strings.

        Security: per-route ``api_key`` values are UPSTREAM provider
        credentials (used to call the routed model's backend), not caller
        authentication -- callers still authenticate with the global
        API_SERVER_KEY bearer token via ``_check_auth``.  Route api_keys must
        never be logged; only alias names and non-secret fields may appear in
        logs.
        """
        if not isinstance(raw, dict):
            if raw:
                logger.warning(
                    "api_server model_routes ignored: expected a mapping, got %s",
                    type(raw).__name__,
                )
            return {}

        allowed_keys = ("model", "provider", "api_key", "base_url")
        routes: Dict[str, Dict[str, Any]] = {}
        for alias, cfg in raw.items():
            alias_str = str(alias).strip()
            if not alias_str or not isinstance(cfg, dict):
                logger.warning(
                    "api_server model_routes: dropping invalid route entry %r", alias_str or alias
                )
                continue
            route = {
                key: str(cfg[key]).strip()
                for key in allowed_keys
                if cfg.get(key) is not None and str(cfg[key]).strip()
            }
            if not route.get("model"):
                logger.warning(
                    "api_server model_routes: route %r has no 'model'; dropping", alias_str
                )
                continue
            routes[alias_str] = route
        return routes

    def _resolve_route(self, model_alias: Any) -> Optional[Dict[str, Any]]:
        """Return the model_routes entry for *model_alias*, or None."""
        if not self._model_routes or not isinstance(model_alias, str):
            return None
        return self._model_routes.get(model_alias)

    def _session_model_override_for(self, session_key: Optional[str]) -> Optional[Dict[str, Any]]:
        """Return the gateway's session ``/model`` override for *session_key*, if any.

        The gateway tracks per-session ``/model`` switches in
        ``GatewayRunner._session_model_overrides``.  API-server requests that
        share such a session key must keep honouring the explicit session
        override even when the request's ``model`` field matches a configured
        route -- a user-issued ``/model`` always wins over static config.
        """
        if not session_key:
            return None
        try:
            from encre.gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
            if runner is None:
                return None
            override = runner._session_model_overrides.get(session_key)
            return dict(override) if isinstance(override, dict) else None
        except Exception:
            return None

    def _create_agent(
        self,
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        gateway_session_key: Optional[str] = None,
        route: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Create an AIAgent instance using the gateway's runtime config.

        Uses _resolve_runtime_agent_kwargs() to pick up model, api_key,
        base_url, etc. from config.yaml / env vars.  Toolsets are resolved
        from config.yaml platform_toolsets.api_server (same as all other
        gateway platforms), falling back to the encre-api-server default.

        ``gateway_session_key`` is a stable per-channel identifier supplied
        by the client (via ``X-Encre-Session-Key``).  Unlike ``session_id``
        which scopes the short-term transcript and rotates on /new, this
        key is meant to persist across transcripts so long-term memory
        providers (e.g. Honcho) can scope their per-chat state correctly
        -- matching the semantics of the native gateway's ``session_key``.

        ``route`` is an optional ``model_routes`` entry (per-client model
        routing).  When set -- and no session ``/model`` override exists for
        this session -- its model/provider/api_key/base_url override the
        global defaults for this agent instance only.
        """
        from run_agent import AIAgent
        from encre.gateway.run import (
            _current_max_iterations,
            _resolve_runtime_agent_kwargs,
            _resolve_gateway_model,
            _load_gateway_config,
            GatewayRunner,
        )
        from encre.tools_config import _get_platform_tools

        runtime_kwargs = _resolve_runtime_agent_kwargs()
        reasoning_config = GatewayRunner._load_reasoning_config()
        model = _resolve_gateway_model()

        # When the primary provider's auth fails (expired token / 429 quota
        # cap), _resolve_runtime_agent_kwargs() falls through to the fallback
        # provider chain, whose runtime dict carries its own ``model`` key.
        # Pop it and let it override the config model, mirroring the native
        # gateway path (_resolve_session_agent_runtime in run.py). Otherwise
        # the explicit ``model=model`` below collides with the ``**runtime_kwargs``
        # spread ->"got multiple values for keyword argument 'model'", 500ing
        # every /v1/chat/completions request while a fallback is active.
        runtime_model = runtime_kwargs.pop("model", None)
        if runtime_model:
            model = runtime_model

        # Per-client model routing (model_routes config).  The route was
        # resolved from the request's ``model`` field by the HTTP handler.
        # Precedence (highest first): session ``/model`` override -> model_routes
        # route -> global config -- an explicit user-issued ``/model`` on the
        # session always beats static per-client route config.
        session_override = self._session_model_override_for(
            gateway_session_key or session_id
        )
        if route and not session_override:
            if route.get("provider"):
                # Resolve real credentials for the routed provider (mirrors
                # the channel_overrides path in gateway/run.py) so a route
                # without an explicit api_key/base_url still gets the right
                # provider auth instead of the default provider's key.
                try:
                    from encre.gateway.run import _resolve_runtime_agent_kwargs_for_provider
                    provider_kwargs = _resolve_runtime_agent_kwargs_for_provider(
                        route["provider"]
                    )
                    provider_kwargs.pop("model", None)
                    runtime_kwargs.update(provider_kwargs)
                except Exception:
                    # Fall back to just switching the provider name; explicit
                    # per-route api_key/base_url below can still complete auth.
                    runtime_kwargs["provider"] = route["provider"]
            if route.get("model"):
                model = route["model"]
            # Per-route secrets are upstream provider credentials. Never log
            # them (compare _check_auth: caller auth stays the global bearer
            # key checked with hmac.compare_digest).
            if route.get("api_key"):
                runtime_kwargs["api_key"] = route["api_key"]
            if route.get("base_url"):
                runtime_kwargs["base_url"] = route["base_url"]
            logger.debug(
                "api_server model route applied: model=%s provider=%s",
                model,
                runtime_kwargs.get("provider"),
            )
        elif route and session_override:
            logger.debug(
                "api_server model route skipped: session /model override wins for %s",
                gateway_session_key or session_id,
            )

        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "api_server"))

        max_iterations = _current_max_iterations()

        # Load fallback provider chain so the API server platform has the
        # same fallback behaviour as Telegram/Discord/Slack (fixes #4954).
        fallback_model = GatewayRunner._load_fallback_model()

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            max_iterations=max_iterations,
            quiet_mode=True,
            verbose_logging=False,
            ephemeral_system_prompt=ephemeral_system_prompt or None,
            enabled_toolsets=enabled_toolsets,
            session_id=session_id,
            platform="api_server",
            stream_delta_callback=stream_delta_callback,
            tool_progress_callback=tool_progress_callback,
            tool_start_callback=tool_start_callback,
            tool_complete_callback=tool_complete_callback,
            session_db=self._ensure_session_db(),
            fallback_model=fallback_model,
            reasoning_config=reasoning_config,
            gateway_session_key=gateway_session_key,
        )
        return agent

    # ------------------------------------------------------------------
    # HTTP Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health -- simple health check."""
        return web.json_response(
            {"status": "ok", "platform": "encre-agent", "version": _encre_version()}
        )

    async def _handle_health_detailed(self, request: "web.Request") -> "web.Response":
        """GET /health/detailed -- rich status for cross-container dashboard probing.

        Returns gateway state, connected platforms, PID, and uptime so the
        dashboard can display full status without needing a shared PID file or
        /proc access.  Requires the same Bearer auth as other API routes.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        from encre.gateway.status import (
            derive_gateway_busy,
            derive_gateway_drainable,
            parse_active_agents,
            read_runtime_status,
        )

        runtime = read_runtime_status() or {}
        gw_state = runtime.get("gateway_state")
        gw_active = parse_active_agents(runtime.get("active_agents", 0))
        # This endpoint is served BY the gateway process, so it is by definition
        # alive -- gateway_running is True. Derive busy/drainable from the same
        # shared contract /api/status uses so the two surfaces never disagree.
        active_api_runs, process_depth, active_delegations = self._readiness_work_counts()
        from encre.gateway.run import _resolve_gateway_model

        readiness = collect_runtime_readiness(
            configured_model=_resolve_gateway_model(),
            runtime_status=runtime,
            active_api_runs=active_api_runs,
            process_completion_queue_depth=process_depth,
            active_delegations=active_delegations,
        )
        return web.json_response({
            "status": readiness["status"],
            "readiness": readiness,
            "platform": "encre-agent",
            "version": _encre_version(),
            "gateway_state": gw_state,
            "platforms": runtime.get("platforms", {}),
            "active_agents": gw_active,
            "gateway_busy": derive_gateway_busy(
                gateway_running=True,
                gateway_state=gw_state,
                active_agents=gw_active,
            ),
            "gateway_drainable": derive_gateway_drainable(
                gateway_running=True,
                gateway_state=gw_state,
            ),
            "exit_reason": runtime.get("exit_reason"),
            "updated_at": runtime.get("updated_at"),
            "pid": os.getpid(),
        })

    async def _handle_models(self, request: "web.Request") -> "web.Response":
        """GET /v1/models -- list encre-agent and any configured model_routes aliases.

        Under ``/p/<profile>/v1/models`` (multiplex on) the advertised primary
        model id follows that profile's name/config, not the default adapter's
        cached ``_model_name``.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        now = int(time.time())
        # Middleware already entered the profile runtime scope when a /p/
        # prefix was present, so get_active_profile_name() resolves correctly.
        model_name = (
            self._resolve_model_name("")
            if _api_request_profile.get()
            else self._model_name
        )
        models = [
            {
                "id": model_name,
                "object": "model",
                "created": now,
                "owned_by": "encre",
                "permission": [],
                "root": model_name,
                "parent": None,
            }
        ]
        # Expose configured model route aliases so clients can discover them.
        # Only the alias and resolved model name are exposed -- never provider
        # credentials.
        for alias, route_cfg in self._model_routes.items():
            if alias == model_name:
                continue  # already listed above
            models.append({
                "id": alias,
                "object": "model",
                "created": now,
                "owned_by": "encre",
                "permission": [],
                "root": route_cfg.get("model", alias),
                "parent": model_name,
            })

        return web.json_response({"object": "list", "data": models})

    async def _handle_capabilities(self, request: "web.Request") -> "web.Response":
        """GET /v1/capabilities -- advertise the stable API surface.

        External UIs and orchestrators use this endpoint to discover the API
        server's plugin-safe contract without scraping docs or assuming that
        every Encre version exposes the same endpoints.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        return web.json_response({
            "object": "encre.api_server.capabilities",
            "platform": "encre-agent",
            "model": self._model_name,
            "auth": {
                "type": "bearer",
                "required": bool(self._api_key),
            },
            "runtime": {
                "mode": "server_agent",
                "tool_execution": "server",
                "split_runtime": False,
                "description": (
                    "The API server creates a server-side Encre AIAgent; "
                    "tools execute on the API-server host unless a future "
                    "explicit split-runtime mode is enabled."
                ),
            },
            "features": {
                "chat_completions": True,
                "chat_completions_streaming": True,
                "responses_api": True,
                "responses_streaming": True,
                "run_submission": True,
                "run_status": True,
                "run_events_sse": True,
                "run_stop": True,
                "run_approval_response": True,
                "tool_progress_events": True,
                "approval_events": True,
                "session_resources": True,
                "session_chat": True,
                "session_chat_streaming": True,
                "session_fork": True,
                "admin_config_rw": False,
                "jobs_admin": False,
                "memory_write_api": False,
                "skills_api": True,
                "audio_api": False,
                "realtime_voice": False,
                "session_continuity_header": "X-Encre-Session-Id",
                "session_key_header": "X-Encre-Session-Key",
                "cors": bool(self._cors_origins),
            },
            "endpoints": {
                "health": {"method": "GET", "path": "/health"},
                "health_detailed": {"method": "GET", "path": "/health/detailed"},
                "models": {"method": "GET", "path": "/v1/models"},
                "chat_completions": {"method": "POST", "path": "/v1/chat/completions"},
                "responses": {"method": "POST", "path": "/v1/responses"},
                "runs": {"method": "POST", "path": "/v1/runs"},
                "run_status": {"method": "GET", "path": "/v1/runs/{run_id}"},
                "run_events": {"method": "GET", "path": "/v1/runs/{run_id}/events"},
                "run_approval": {"method": "POST", "path": "/v1/runs/{run_id}/approval"},
                "run_stop": {"method": "POST", "path": "/v1/runs/{run_id}/stop"},
                "skills": {"method": "GET", "path": "/v1/skills"},
                "toolsets": {"method": "GET", "path": "/v1/toolsets"},
                "sessions": {"method": "GET", "path": "/api/sessions"},
                "session_create": {"method": "POST", "path": "/api/sessions"},
                "session": {"method": "GET", "path": "/api/sessions/{session_id}"},
                "session_update": {"method": "PATCH", "path": "/api/sessions/{session_id}"},
                "session_delete": {"method": "DELETE", "path": "/api/sessions/{session_id}"},
                "session_messages": {"method": "GET", "path": "/api/sessions/{session_id}/messages"},
                "session_fork": {"method": "POST", "path": "/api/sessions/{session_id}/fork"},
                "session_chat": {"method": "POST", "path": "/api/sessions/{session_id}/chat"},
                "session_chat_stream": {"method": "POST", "path": "/api/sessions/{session_id}/chat/stream"},
            },
        })

    async def _handle_skills(self, request: "web.Request") -> "web.Response":
        """GET /v1/skills -- list installed skills visible to the API-server agent.

        Read-only listing intended for external clients that need to know
        which skills are available without sending a chat message and asking
        the model. Mirrors what the gateway/CLI surfaces through
        ``/skills list``, but as a deterministic JSON payload.

        Returns the same skill metadata (name, description, category) the
        skills hub uses internally. Disabled skills are excluded so the
        listing matches what the agent actually loads.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            from tools.skills_tool import _find_all_skills, _sort_skills
            skills = _sort_skills(_find_all_skills(skip_disabled=False))
        except Exception:
            logger.exception("GET /v1/skills failed")
            return web.json_response(
                _openai_error("Failed to enumerate skills", err_type="server_error"),
                status=500,
            )

        return web.json_response({
            "object": "list",
            "data": skills,
        })

    async def _handle_toolsets(self, request: "web.Request") -> "web.Response":
        """GET /v1/toolsets -- list toolsets and their resolved tools.

        Returns the toolset surface the api_server platform actually exposes
        to its agent: each toolset's enabled/configured state plus the
        concrete tool names it expands to. This is the deterministic
        equivalent of what a client would otherwise have to recover by
        asking the model what tools it can call.
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        try:
            from encre.config import load_config
            from encre.tools_config import (
                _get_effective_configurable_toolsets,
                _get_platform_tools,
                _toolset_has_keys,
            )
            from toolsets import resolve_toolset

            config = load_config()
            enabled_toolsets = _get_platform_tools(
                config,
                "api_server",
                include_default_mcp_servers=False,
            )
            data: List[Dict[str, Any]] = []
            for name, label, desc in _get_effective_configurable_toolsets():
                try:
                    tools = sorted(set(resolve_toolset(name)))
                except Exception:
                    tools = []
                is_enabled = name in enabled_toolsets
                data.append({
                    "name": name,
                    "label": label,
                    "description": desc,
                    "enabled": is_enabled,
                    "configured": _toolset_has_keys(name, config),
                    "tools": tools,
                })
        except Exception:
            logger.exception("GET /v1/toolsets failed")
            return web.json_response(
                _openai_error("Failed to enumerate toolsets", err_type="server_error"),
                status=500,
            )

        return web.json_response({
            "object": "list",
            "platform": "api_server",
            "data": data,
        })

    # ------------------------------------------------------------------
    # /api/sessions -- thin client/session resource API
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_nonnegative_int(value: Any, default: int, maximum: int) -> int:
        """Coerce *value* to an int in ``[0, maximum]``, else return *default*.

        A malformed, non-int, or negative value falls back to *default* instead
        of raising, so a hostile or mistaken query string cannot 500 the
        handler. The result is additionally clamped to *maximum* to bound work.

        Args:
            value: Raw query-string value.
            default: Value used when parsing fails or the result is negative.
            maximum: Upper bound the result is clamped to.

        Returns:
            The clamped integer.
        """
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        if parsed < 0:
            return default
        return min(parsed, maximum)

    @staticmethod
    def _session_response(session: Dict[str, Any]) -> Dict[str, Any]:
        """Return a stable, client-safe session representation."""
        safe_keys = (
            "id", "source", "user_id", "model", "title", "started_at", "ended_at",
            "end_reason", "message_count", "tool_call_count", "input_tokens",
            "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd",
            "api_call_count", "parent_session_id", "last_active", "preview",
            "_lineage_root_id",
        )
        payload = {key: session.get(key) for key in safe_keys if key in session}
        # Avoid exposing full system prompts/model_config through the client API;
        # callers only need to know whether those snapshots exist.
        payload["has_system_prompt"] = bool(session.get("system_prompt"))
        payload["has_model_config"] = bool(session.get("model_config"))
        return payload

    @staticmethod
    def _message_response(message: Dict[str, Any]) -> Dict[str, Any]:
        """Return only the client-safe subset of a message record.

        Mirrors :meth:`_session_response`: the allowlist drops internal fields
        so a message stored with extra keys cannot leak them through the API.

        Args:
            message: A message row loaded from the session database.

        Returns:
            A new dict holding only the allowlisted keys that were present.
        """
        safe_keys = (
            "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
            "tool_name", "timestamp", "token_count", "finish_reason", "reasoning",
            "reasoning_content",
        )
        return {key: message.get(key) for key in safe_keys if key in message}

    async def _read_json_body(self, request: "web.Request") -> tuple[Dict[str, Any], Optional["web.Response"]]:
        """Parse the request body as a JSON object.

        Centralises the two checks every write handler needs so they cannot
        drift: malformed JSON and a non-object top level both become a 400
        rather than an AttributeError deeper in the handler.

        Returns:
            ``(body, None)`` on success, or ``({}, error_response)`` when the
            body is malformed or not a JSON object.
        """
        try:
            body = await request.json()
        except Exception:
            return {}, web.json_response(_openai_error("Invalid JSON in request body"), status=400)
        if not isinstance(body, dict):
            return {}, web.json_response(_openai_error("Request body must be a JSON object"), status=400)
        return body, None

    async def _get_existing_session_or_404(self, session_id: str) -> tuple[Optional[Dict[str, Any]], Optional["web.Response"]]:
        """Load a session by id, or build the error response instead.

        Returns:
            ``(session, None)`` on success, or ``(None, error_response)``:
            503 when the session database cannot be opened, 404 when the id
            does not exist.
        """
        db = await self._ensure_session_db_async()
        if db is None:
            return None, web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)
        # Offload the blocking SQLite read off the event loop (CWE/perf: the
        # API server is single-threaded aiohttp; a sync SessionDB call here
        # freezes every in-flight request, see PR discussion on event-loop
        # blocking SQLite in the gateway surface).
        session = await asyncio.to_thread(db.get_session, session_id)
        if not session:
            return None, web.json_response(_openai_error(f"Session not found: {session_id}", code="session_not_found"), status=404)
        return session, None

    async def _conversation_history_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        """Return a session's messages as a chat-format conversation list.

        Failures are logged and turned into an empty list rather than raised,
        because a caller using this to seed an agent turn should still be able
        to run the turn with no history instead of surfacing a 500.

        Args:
            session_id: Session whose history to load.

        Returns:
            The conversation list, or ``[]`` if the database is unavailable or
            the read fails.
        """
        db = await self._ensure_session_db_async()
        if db is None:
            return []
        try:
            return await asyncio.to_thread(db.get_messages_as_conversation, session_id)
        except Exception as exc:
            logger.warning("Failed to load session history for %s: %s", session_id, exc)
            return []

    async def _handle_list_sessions(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions -- list persisted Encre sessions."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        db = await self._ensure_session_db_async()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        limit = self._parse_nonnegative_int(request.query.get("limit"), default=50, maximum=200)
        offset = self._parse_nonnegative_int(request.query.get("offset"), default=0, maximum=1_000_000)
        source = request.query.get("source") or None
        include_children = _coerce_request_bool(request.query.get("include_children"), default=False)
        sessions = await asyncio.to_thread(db.list_sessions_rich,
            source=source,
            limit=limit,
            offset=offset,
            include_children=include_children,
            order_by_last_active=True,
        )
        return web.json_response({
            "object": "list",
            "data": [self._session_response(s) for s in sessions],
            "limit": limit,
            "offset": offset,
            "has_more": len(sessions) == limit,
        })

    async def _handle_create_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions -- create an empty Encre session row.

        The existence check, insert, title handling, and invalid-title
        rollback run as a single off-loop operation to avoid a TOCTOU
        window between the duplicate check and the insert (concurrent
        same-ID creates could otherwise both pass the check and both
        return 201 via the ON CONFLICT enrichment upsert).
        """
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        body, err = await self._read_json_body(request)
        if err:
            return err

        db = await self._ensure_session_db_async()
        if db is None:
            return web.json_response(_openai_error("Session database unavailable", code="session_db_unavailable"), status=503)

        raw_id = body.get("id") or body.get("session_id")
        session_id = str(raw_id).strip() if raw_id else f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        from encre.gateway.session import _is_path_unsafe
        if not session_id or re.search(r'[\r\n\x00]', session_id) or _is_path_unsafe(session_id):
            return web.json_response(_openai_error("Invalid session ID", code="invalid_session_id"), status=400)
        if len(session_id) > self._MAX_SESSION_HEADER_LEN:
            return web.json_response(_openai_error("Session ID too long", code="invalid_session_id"), status=400)

        model = body.get("model") or self._model_name
        system_prompt = body.get("system_prompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_prompt must be a string", code="invalid_system_prompt"), status=400)
        title = body.get("title")

        # Run the entire check-insert-title sequence inside a single
        # _execute_write call (BEGIN IMMEDIATE + commit) so the existence
        # check and the insert are atomic at the SQLite level.  Two
        # concurrent requests for the same ID serialize here: the second
        # one blocks on the write lock and sees the row the first inserted.
        def _do_create():
            """Create the session row inside one atomic SQLite write."""
            def _atomic(conn):
                """Insert the session, enforcing id and title uniqueness.

                Returns:
                    ``(session_row, None)`` on success, or ``(None, "exists")``
                    for a duplicate id, or ``(None, "title:...")`` when the
                    chosen title is already taken. In the title-conflict case
                    the just-inserted row is deleted so the transaction leaves
                    no partial state.
                """
                row = conn.execute(
                    "SELECT id FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row:
                    return None, "exists"
                import time as _time
                conn.execute(
                    """INSERT INTO sessions (
                       id, source, model, system_prompt, started_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        "api_server",
                        str(model) if model else None,
                        system_prompt,
                        _time.time(),
                    ),
                )
                if title is not None:
                    clean_title = db.sanitize_title(str(title))
                    if clean_title:
                        conflict = conn.execute(
                            "SELECT id FROM sessions WHERE title = ? AND id != ?",
                            (clean_title, session_id),
                        ).fetchone()
                        if conflict:
                            conn.execute(
                                "DELETE FROM sessions WHERE id = ?", (session_id,)
                            )
                            return None, f"title:Title already in use by session {conflict['id']}"
                    conn.execute(
                        "UPDATE sessions SET title = ? WHERE id = ?",
                        (clean_title, session_id),
                    )
                session_row = conn.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                return (dict(session_row) if session_row else {
                    "id": session_id, "source": "api_server",
                    "model": model, "title": title,
                }), None
            return db._execute_write(_atomic)

        session, err = await asyncio.to_thread(_do_create)
        if err == "exists":
            return web.json_response(_openai_error(f"Session already exists: {session_id}", code="session_exists"), status=409)
        if err and err.startswith("title:"):
            return web.json_response(_openai_error(err[len("title:"):], code="invalid_title"), status=400)
        return web.json_response({"object": "encre.session", "session": self._session_response(session)}, status=201)

    async def _handle_get_session(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session, err = await self._get_existing_session_or_404(request.match_info["session_id"])
        if err:
            return err
        return web.json_response({"object": "encre.session", "session": self._session_response(session)})

    async def _handle_patch_session(self, request: "web.Request") -> "web.Response":
        """PATCH /api/sessions/{session_id} -- update client-safe session metadata."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        allowed = {"title", "end_reason"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            return web.json_response(_openai_error(f"Unsupported session fields: {', '.join(unknown)}", code="unsupported_session_field"), status=400)

        db = await self._ensure_session_db_async()
        if "title" in body:
            try:
                await asyncio.to_thread(db.set_session_title, session_id, "" if body["title"] is None else str(body["title"]))
            except ValueError as exc:
                return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        if body.get("end_reason"):
            await asyncio.to_thread(db.end_session, session_id, str(body["end_reason"]))
        session = await asyncio.to_thread(db.get_session, session_id) or session
        return web.json_response({"object": "encre.session", "session": self._session_response(session)})

    async def _handle_delete_session(self, request: "web.Request") -> "web.Response":
        """DELETE /api/sessions/{session_id}."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        session, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        db = await self._ensure_session_db_async()
        deleted = await asyncio.to_thread(db.delete_session, session_id)
        return web.json_response({"object": "encre.session.deleted", "id": session_id, "deleted": bool(deleted)})

    async def _handle_session_messages(self, request: "web.Request") -> "web.Response":
        """GET /api/sessions/{session_id}/messages."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        session_id = request.match_info["session_id"]
        _, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        db = await self._ensure_session_db_async()
        resolved_id = await asyncio.to_thread(db.resolve_resume_session_id, session_id)
        messages = await asyncio.to_thread(db.get_messages, resolved_id)
        return web.json_response({
            "object": "list",
            "session_id": resolved_id,
            "data": [self._message_response(m) for m in messages],
        })

    async def _handle_fork_session(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/fork -- branch via current SessionDB primitives."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        source_id = request.match_info["session_id"]
        source, err = await self._get_existing_session_or_404(source_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        db = await self._ensure_session_db_async()
        fork_id = str(body.get("id") or body.get("session_id") or f"api_{int(time.time())}_{uuid.uuid4().hex[:8]}").strip()
        if not fork_id or re.search(r'[\r\n\x00]', fork_id):
            return web.json_response(_openai_error("Invalid session ID", code="invalid_session_id"), status=400)
        if await asyncio.to_thread(db.get_session, fork_id):
            return web.json_response(_openai_error(f"Session already exists: {fork_id}", code="session_exists"), status=409)

        # Match the CLI /branch semantics: mark the original as branched, then
        # create a child session that carries the transcript forward. This uses
        # SessionDB's native parent_session_id/end_reason visibility model rather
        # than inventing a parallel fork store.
        await asyncio.to_thread(db.end_session, source_id, "branched")
        await asyncio.to_thread(db.create_session,
            fork_id,
            "api_server",
            model=source.get("model"),
            system_prompt=source.get("system_prompt"),
            parent_session_id=source_id,
        )
        messages = await asyncio.to_thread(db.get_messages, source_id)
        await asyncio.to_thread(db.replace_messages, fork_id, messages)
        title = body.get("title")
        if title is None:
            base = source.get("title") or "fork"
            try:
                title = await asyncio.to_thread(db.get_next_title_in_lineage, base)
            except Exception:
                title = f"{base} fork"
        try:
            await asyncio.to_thread(db.set_session_title, fork_id, str(title))
        except ValueError as exc:
            return web.json_response(_openai_error(str(exc), code="invalid_title"), status=400)
        fork = await asyncio.to_thread(db.get_session, fork_id) or {"id": fork_id, "parent_session_id": source_id}
        return web.json_response({"object": "encre.session", "session": self._session_response(fork)}, status=201)

    @_admit_api_agent_request
    async def _handle_session_chat(self, request: "web.Request") -> "web.Response":
        """POST /api/sessions/{session_id}/chat -- one synchronous agent turn."""
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        session_id = request.match_info["session_id"]
        _, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)
        history = await self._conversation_history_for_session(session_id)
        result, usage = await self._run_agent(
            user_message=user_message,
            conversation_history=history,
            ephemeral_system_prompt=system_prompt,
            session_id=session_id,
            gateway_session_key=gateway_session_key,
        )
        effective_session_id = result.get("session_id") if isinstance(result, dict) else session_id
        final_response = _resolve_media_to_data_urls(result.get("final_response", "") if isinstance(result, dict) else "")
        headers = {"X-Encre-Session-Id": effective_session_id or session_id}
        if gateway_session_key:
            headers["X-Encre-Session-Key"] = gateway_session_key
        return web.json_response(
            {
                "object": "encre.session.chat.completion",
                "session_id": effective_session_id or session_id,
                "message": {"role": "assistant", "content": final_response},
                "usage": usage,
            },
            headers=headers,
        )

    @_admit_api_agent_request
    async def _handle_session_chat_stream(self, request: "web.Request") -> "web.StreamResponse":
        """POST /api/sessions/{session_id}/chat/stream -- SSE wrapper over _run_agent."""
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err
        session_id = request.match_info["session_id"]
        _, err = await self._get_existing_session_or_404(session_id)
        if err:
            return err
        body, err = await self._read_json_body(request)
        if err:
            return err
        user_message, err = _session_chat_user_message(body)
        if err is not None:
            return err
        system_prompt = body.get("system_message") or body.get("instructions")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return web.json_response(_openai_error("system_message must be a string", code="invalid_system_message"), status=400)

        loop = asyncio.get_running_loop()
        queue: "asyncio.Queue[Optional[tuple[str, Dict[str, Any]]]]" = asyncio.Queue()
        message_id = f"msg_{uuid.uuid4().hex}"
        run_id = f"run_{uuid.uuid4().hex}"
        seq = 0

        def _event_payload(name: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
            """Stamp *payload* with run identity and the next sequence number.

            Uses ``setdefault`` so an event that already carries an explicit
            value keeps it, which lets terminal events override the default
            session id with the effective (possibly rotated) one.
            """
            nonlocal seq
            seq += 1
            payload.setdefault("session_id", session_id)
            payload.setdefault("run_id", run_id)
            payload.setdefault("seq", seq)
            payload.setdefault("ts", time.time())
            return name, payload

        def _enqueue(name: str, payload: Dict[str, Any]) -> None:
            """Put an event on the stream queue, thread-safely if needed.

            Callbacks may fire from a worker thread, so the event is posted
            back onto the owning loop with ``call_soon_threadsafe`` unless it
            is already running there. A closed loop drops the event silently:
            by then the response is gone and there is no one to deliver to.
            """
            event = _event_payload(name, payload)
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            try:
                if running_loop is loop:
                    queue.put_nowait(event)
                else:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass

        def _delta(delta: str) -> None:
            """Forward a non-empty text delta as an ``assistant.delta`` event."""
            if delta:
                _enqueue("assistant.delta", {"message_id": message_id, "delta": delta})

        def _tool_progress(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs        ) -> None:
            """Map an internal progress event onto a wire event name.

            Reasoning is relabelled as a pseudo-tool named ``_thinking`` so a
            client that only understands tool progress can still render it, and
            the three ``tool.*`` lifecycle events pass through under their own
            names.
            """
            if event_type == "reasoning.available":
                _enqueue("tool.progress", {"message_id": message_id, "tool_name": tool_name or "_thinking", "delta": preview or ""})
            elif event_type in {"tool.started", "tool.completed", "tool.failed"}:
                event_name = event_type.replace("tool.", "tool.")
                _enqueue(event_name, {"message_id": message_id, "tool_name": tool_name, "preview": preview, "args": args})

        async def _run_and_signal() -> None:
            """Run the agent and push the full SSE lifecycle to the queue.

            Emits ``run.started`` and ``message.started`` before the turn so a
            client can begin rendering immediately, then the completed events
            after. ``done`` and the ``None`` sentinel are always queued from
            ``finally``, so the reader loop terminates even on failure.
            """
            try:
                await queue.put(_event_payload("run.started", {"user_message": {"role": "user", "content": user_message}}))
                await queue.put(_event_payload("message.started", {"message": {"id": message_id, "role": "assistant"}}))
                history = await self._conversation_history_for_session(session_id)
                result, usage = await self._run_agent(
                    user_message=user_message,
                    conversation_history=history,
                    ephemeral_system_prompt=system_prompt,
                    session_id=session_id,
                    stream_delta_callback=_delta,
                    tool_progress_callback=_tool_progress,
                    gateway_session_key=gateway_session_key,
                )
                final_response = _resolve_media_to_data_urls(result.get("final_response", "") if isinstance(result, dict) else "")
                effective_session_id = result.get("session_id", session_id) if isinstance(result, dict) else session_id
                turn_messages = self._turn_transcript_messages(history, user_message, result) if isinstance(result, dict) else []
                await queue.put(_event_payload("assistant.completed", {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "content": final_response,
                    "completed": True,
                    "partial": False,
                    "interrupted": False,
                }))
                await queue.put(_event_payload("run.completed", {
                    "session_id": effective_session_id,
                    "message_id": message_id,
                    "completed": True,
                    "messages": turn_messages,
                    "usage": usage,
                }))
            except Exception as exc:
                logger.exception("[api_server] session chat stream failed")
                await queue.put(_event_payload("error", {"message": _redact_api_error_text(exc)}))
            finally:
                await queue.put(_event_payload("done", {}))
                await queue.put(None)

        task = asyncio.create_task(_run_and_signal())
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Encre-Session-Id": session_id,
        }
        if gateway_session_key:
            headers["X-Encre-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=headers)
        await response.prepare(request)
        last_write = time.monotonic()
        try:
            # A timeout writes a comment-only SSE frame rather than closing: a
            # long agent turn can go minutes without a delta, and a silent
            # socket is indistinguishable from a dead one behind a proxy that
            # drops idle connections.
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    last_write = time.monotonic()
                    continue
                if item is None:
                    break
                name, payload = item
                data = json.dumps(payload, ensure_ascii=False)
                await response.write(f"event: {name}\ndata: {data}\n\n".encode("utf-8"))
                last_write = time.monotonic()
        except (asyncio.CancelledError, ConnectionResetError):
            task.cancel()
            raise
        except Exception as exc:
            logger.debug("[api_server] session SSE stream error: %s", exc)
        return response

    @_admit_api_agent_request
    async def _handle_chat_completions(self, request: "web.Request") -> "web.Response":
        """POST /v1/chat/completions -- OpenAI Chat Completions format."""
        # Bound total in-flight agent runs (configurable; #7483).
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(_openai_error("Invalid JSON in request body"), status=400)

        messages = body.get("messages")
        if not messages or not isinstance(messages, list):
            return web.json_response(
                {"error": {"message": "Missing or invalid 'messages' field", "type": "invalid_request_error"}},
                status=400,
            )

        stream = _coerce_request_bool(body.get("stream"), default=False)

        # Extract system message (becomes ephemeral system prompt layered ON TOP of core)
        system_prompt = None
        conversation_messages: List[Dict[str, str]] = []

        for idx, msg in enumerate(messages):
            role = msg.get("role", "")
            raw_content = msg.get("content", "")
            if role == "system":
                # System messages don't support images (Anthropic rejects, OpenAI
                # text-model systems don't render them).  Flatten to text.
                content = _normalize_chat_content(raw_content)
                if system_prompt is None:
                    system_prompt = content
                else:
                    system_prompt = system_prompt + "\n" + content
            elif role in {"user", "assistant"}:
                try:
                    content = _normalize_multimodal_content(raw_content)
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"messages[{idx}].content")
                conversation_messages.append({"role": role, "content": content})

        # Extract the last user message as the primary input
        user_message: Any = ""
        history = []
        if conversation_messages:
            user_message = conversation_messages[-1].get("content", "")
            history = conversation_messages[:-1]

        if not _content_has_visible_payload(user_message):
            return web.json_response(
                {"error": {"message": "No user message found in messages", "type": "invalid_request_error"}},
                status=400,
            )

        # Allow caller to scope long-term memory (e.g. Honcho) with a
        # stable per-channel identifier via X-Encre-Session-Key.  This
        # is independent of X-Encre-Session-Id: the key persists across
        # transcripts while the id rotates when the caller starts a new
        # transcript (i.e. /new semantics).  See _parse_session_key_header.
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        # Allow caller to continue an existing session by passing X-Encre-Session-Id.
        # When provided, history is loaded from state.db instead of from the request body.
        #
        # Security: session continuation exposes conversation history, so it is
        # only allowed when the API key is configured and the request is
        # authenticated.  Without this gate, any unauthenticated client could
        # read arbitrary session history by guessing/enumerating session IDs.
        provided_session_id = request.headers.get("X-Encre-Session-Id", "").strip()
        if provided_session_id:
            if not self._api_key:
                logger.warning(
                    "Session continuation via X-Encre-Session-Id rejected: "
                    "no API key configured.  Set API_SERVER_KEY to enable "
                    "session continuity."
                )
                return web.json_response(
                    _openai_error(
                        "Session continuation requires API key authentication. "
                        "Configure API_SERVER_KEY to enable this feature."
                    ),
                    status=403,
                )
            # Sanitize: reject control characters that could enable header
            # injection, and path-traversal-shaped IDs that would escape the
            # sessions directory when interpolated into on-disk artifact
            # filenames (session snapshots, request dumps). Mirrors the native
            # gateway's entry-boundary guard (encre.gateway.session._is_path_unsafe).
            from encre.gateway.session import _is_path_unsafe
            if re.search(r'[\r\n\x00]', provided_session_id) or _is_path_unsafe(provided_session_id):
                return web.json_response(
                    {"error": {"message": "Invalid session ID", "type": "invalid_request_error"}},
                    status=400,
                )
            if len(provided_session_id) > self._MAX_SESSION_HEADER_LEN:
                return web.json_response(
                    {"error": {"message": "Session ID too long", "type": "invalid_request_error"}},
                    status=400,
                )
            session_id = provided_session_id
            try:
                db = await self._ensure_session_db_async()
                if db is not None:
                    history = await asyncio.to_thread(db.get_messages_as_conversation, session_id)
            except Exception as e:
                logger.warning("Failed to load session history for %s: %s", session_id, e)
                history = []
        else:
            # Derive a stable session ID from the conversation fingerprint so
            # that consecutive messages from the same Open WebUI (or similar)
            # conversation map to the same Encre session.  The first user
            # message + system prompt are constant across all turns.
            first_user = ""
            for cm in conversation_messages:
                if cm.get("role") == "user":
                    first_user = cm.get("content", "")
                    break
            session_id = _derive_chat_session_id(system_prompt, first_user)
            # history already set from request body above

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
        model_name = body.get("model", self._model_name)
        created = int(time.time())

        # Per-client model routing: if the requested model matches a
        # configured model_routes alias, this request's agent is created
        # with that route's model/provider instead of the global default.
        route = self._resolve_route(model_name)

        if stream:
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                """Queue a text delta; drop the agent's ``None`` signal."""
                # Filter out None -- the agent fires stream_delta_callback(None)
                # to signal the CLI display to close its response box before
                # tool execution, but the SSE writer uses None as end-of-stream
                # sentinel.  Forwarding it would prematurely close the HTTP
                # response, causing Open WebUI (and similar frontends) to miss
                # the final answer after tool calls.  The SSE loop detects
                # completion via agent_task.done() instead.
                if delta is not None:
                    _stream_q.put(delta)

            # Track which tool_call_ids we've emitted a "running" lifecycle
            # event for, so a "completed" event without a matching "running"
            # (e.g. internal/filtered tools) is silently dropped instead of
            # producing an orphaned event clients can't correlate.
            _started_tool_call_ids: set[str] = set()

            def _on_tool_start(tool_call_id, function_name, function_args):
                """Emit ``encre.tool.progress`` with ``status: running``.

                Replaces the old ``tool_progress_callback("tool.started",
                ...)`` emit so SSE consumers receive a single event per
                tool start, carrying both the legacy ``tool``/``emoji``/
                ``label`` payload (for #6972 frontends) and the new
                ``toolCallId``/``status`` correlation fields (#16588).

                Skips tools whose names start with ``_`` so internal
                events (``_thinking``, -- stay off the wire -- matching
                the prior ``_on_tool_progress`` filter exactly.
                """
                if not tool_call_id or function_name.startswith("_"):
                    return
                _started_tool_call_ids.add(tool_call_id)
                from agent.display import build_tool_preview, get_tool_emoji
                label = build_tool_preview(function_name, function_args) or function_name
                _stream_q.put(("__tool_progress__", {
                    "tool": function_name,
                    "emoji": get_tool_emoji(function_name),
                    "label": label,
                    "toolCallId": tool_call_id,
                    "status": "running",
                }))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """Emit the matching ``status: completed`` event.

                Dropped if the start was filtered (internal tool, missing
                id, or never seen) so clients never get an orphaned
                ``completed`` they can't correlate to a prior ``running``.
                """
                if not tool_call_id or tool_call_id not in _started_tool_call_ids:
                    return
                _started_tool_call_ids.discard(tool_call_id)
                _stream_q.put(("__tool_progress__", {
                    "tool": function_name,
                    "toolCallId": tool_call_id,
                    "status": "completed",
                }))

            # Start agent in background.  agent_ref is a mutable container
            # so the SSE writer can interrupt the agent on client disconnect.
            #
            # ``tool_progress_callback`` is intentionally not wired here:
            # it would duplicate every emit because ``run_agent`` fires it
            # side-by-side with ``tool_start_callback``/``tool_complete_callback``.
            # The structured callbacks are strictly richer (they carry
            # the tool_call id), so they own the chat-completions SSE channel.
            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                agent_ref=agent_ref,
                gateway_session_key=gateway_session_key,
                route=route,
            ))
            # Ensure SSE drain loops can terminate without relying on polling
            # agent_task.done(), which can race with queue timeout checks.
            agent_task.add_done_callback(lambda _fut: _stream_q.put(None))

            return await self._write_sse_chat_completion(
                request, completion_id, model_name, created, _stream_q,
                agent_task, agent_ref, session_id=session_id,
                gateway_session_key=gateway_session_key,
            )

        # Non-streaming: run the agent (with optional Idempotency-Key)
        async def _compute_completion():
            """Run the non-streaming agent turn for chat completions."""
            return await self._run_agent(
                user_message=user_message,
                conversation_history=history,
                ephemeral_system_prompt=system_prompt,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                route=route,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(body, keys=["model", "messages", "tools", "tool_choice", "stream"])
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_completion)
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_completion()
            except Exception as e:
                logger.error("Error running agent for chat completions: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = _resolve_media_to_data_urls(result.get("final_response") or "")
        is_partial = bool(result.get("partial"))
        is_failed = bool(result.get("failed"))
        completed = bool(result.get("completed", True))
        raw_err_msg = result.get("error")
        err_msg = _redact_api_error_text(raw_err_msg) if raw_err_msg else raw_err_msg

        # Decide finish_reason. OpenAI uses "length" for truncation, "stop"
        # for normal completion, and downstream SDKs accept "error" / custom
        # codes. See issue #22496.
        if is_partial and err_msg and "truncat" in err_msg.lower():
            finish_reason = "length"
        elif is_failed or (not completed and err_msg):
            finish_reason = "error"
        else:
            finish_reason = "stop"

        response_headers = {
            "X-Encre-Session-Id": result.get("session_id", session_id),
        }
        if gateway_session_key:
            response_headers["X-Encre-Session-Key"] = gateway_session_key

        # Hard-fail path: no usable assistant text AND a real failure -> 5xx
        # with OpenAI-style error envelope so SDK clients raise instead of
        # silently rendering the internal failure string as message.content.
        if not final_response and (is_failed or is_partial):
            err_body = _openai_error(
                err_msg or "Agent run did not produce a response.",
                err_type="server_error",
                code="agent_incomplete",
            )
            err_body["error"]["encre"] = {
                "completed": completed,
                "partial": is_partial,
                "failed": is_failed,
            }
            response_headers["X-Encre-Completed"] = "false"
            response_headers["X-Encre-Partial"] = "true" if is_partial else "false"
            return web.json_response(err_body, status=502, headers=response_headers)

        # Soft-partial path: we have *some* text but the run did not complete
        # (e.g. truncation with partial buffered output). Still 200 but signal
        # truncation via finish_reason="length" + Encre-specific extras.
        response_data = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": final_response,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }
        if is_partial or is_failed or not completed:
            response_data["encre"] = {
                "completed": completed,
                "partial": is_partial,
                "failed": is_failed,
                "error": err_msg,
                "error_code": "output_truncated" if finish_reason == "length" else "agent_error",
            }
            response_headers["X-Encre-Completed"] = "false"
            response_headers["X-Encre-Partial"] = "true" if is_partial else "false"
            if err_msg:
                response_headers["X-Encre-Error"] = _redact_api_error_text(err_msg, limit=200)

        return web.json_response(response_data, headers=response_headers)

    async def _write_sse_chat_completion(
        self, request: "web.Request", completion_id: str, model: str,
        created: int, stream_q, agent_task, agent_ref=None, session_id: str = None,
        gateway_session_key: str = None,
    ) -> "web.StreamResponse":
        """Write real streaming SSE from agent's stream_delta_callback queue.

        If the client disconnects mid-stream (network drop, browser tab close),
        the agent is interrupted via ``agent.interrupt()`` so it stops making
        LLM API calls, and the asyncio task wrapper is cancelled.
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        # CORS middleware can't inject headers into StreamResponse after
        # prepare() flushes them, so resolve CORS headers up front.
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Encre-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Encre-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        try:
            last_activity = time.monotonic()

            # Role chunk
            role_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())
            last_activity = time.monotonic()

            # Helper -- route a queue item to the correct SSE event.
            async def _emit(item):
                """Write a single queue item to the SSE stream.

                Plain strings are sent as normal ``delta.content`` chunks.
                Tagged tuples ``("__tool_progress__", payload)`` are sent
                as a custom ``event: encre.tool.progress`` SSE event so
                frontends can display them without storing the markers in
                conversation history.  See #6972 for the original event,
                #16588 for the ``toolCallId``/``status`` lifecycle fields.
                """
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "__tool_progress__":
                    event_data = json.dumps(item[1])
                    await response.write(
                        f"event: encre.tool.progress\ndata: {event_data}\n\n".encode()
                    )
                else:
                    content_chunk = {
                        "id": completion_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {"content": item}, "finish_reason": None}],
                    }
                    await response.write(f"data: {json.dumps(content_chunk)}\n\n".encode())
                return time.monotonic()

            # Stream content chunks as they arrive from the agent
            loop = asyncio.get_running_loop()
            while True:
                try:
                    delta = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain any remaining items
                        while True:
                            try:
                                delta = stream_q.get_nowait()
                                if delta is None:
                                    break
                                last_activity = await _emit(delta)
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if delta is None:  # End of stream sentinel
                    break

                last_activity = await _emit(delta)

            # Get usage from completed agent. The agent can fail two ways
            # after the content queue terminates cleanly: (1) ``agent_task``
            # raises, or (2) it returns a ``result`` dict flagged
            # failed/partial/incomplete. Both previously fell through to a
            # ``finish_reason: "stop"`` chunk, so OpenAI-compatible clients
            # saw a fake success. Surface either as a non-"stop" finish so
            # the failure is detectable -- mirroring the non-streaming path's
            # decision logic (see the finish_reason block above).
            usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            result = None
            agent_error = None
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
            except Exception as exc:
                agent_error = exc
                logger.error(
                    "Agent task %s failed during SSE streaming: %s", completion_id, exc
                )

            # Inspect the result dict for a flagged (non-exception) failure.
            is_partial = bool(result.get("partial")) if isinstance(result, dict) else False
            is_failed = bool(result.get("failed")) if isinstance(result, dict) else False
            completed = bool(result.get("completed", True)) if isinstance(result, dict) else True
            err_msg = result.get("error") if isinstance(result, dict) else None
            if agent_error is not None:
                is_failed = True
                err_msg = err_msg or str(agent_error)

            # Decide finish_reason, matching the non-streaming logic: "length"
            # for truncation, "error" for failure, "stop" for normal completion.
            if is_partial and err_msg and "truncat" in err_msg.lower():
                finish_reason = "length"
            elif agent_error is not None or is_failed or (not completed and err_msg):
                finish_reason = "error"
            else:
                finish_reason = "stop"

            # Finish chunk
            finish_chunk = {
                "id": completion_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                },
            }
            if finish_reason != "stop":
                finish_chunk["choices"][0]["delta"] = {}
                if err_msg:
                    finish_chunk["error"] = {
                        "message": err_msg,
                        "type": type(agent_error).__name__ if agent_error else "agent_error",
                    }
                finish_chunk["encre"] = {
                    "completed": completed,
                    "partial": is_partial,
                    "failed": is_failed,
                    "error": err_msg,
                    "error_code": "output_truncated" if finish_reason == "length" else "agent_error",
                }
            await response.write(f"data: {json.dumps(finish_chunk)}\n\n".encode())
            await response.write(b"data: [DONE]\n\n")
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            # Client disconnected mid-stream.  Interrupt the agent so it
            # stops making LLM API calls at the next loop iteration, then
            # cancel the asyncio task wrapper.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", completion_id)
        except Exception as _exc:
            # Agent crashed mid-stream.  Try to emit an error chunk
            # so the client gets a proper response instead of a
            # TransferEncodingError from incomplete chunked encoding.
            import traceback as _tb
            logger.error("Agent crashed mid-stream for %s: %s", completion_id, _tb.format_exc()[:300])
            try:
                error_chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created, "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                }
                await response.write(f"data: {json.dumps(error_chunk)}\n\n".encode())
                await response.write(b"data: [DONE]\n\n")
            except Exception:
                pass

        return response

    async def _write_sse_responses(
        self,
        request: "web.Request",
        response_id: str,
        model: str,
        created_at: int,
        stream_q,
        agent_task,
        agent_ref,
        conversation_history: List[Dict[str, str]],
        user_message: str,
        instructions: Optional[str],
        conversation: Optional[str],
        store: bool,
        session_id: str,
        gateway_session_key: Optional[str] = None,
    ) -> "web.StreamResponse":
        """Write an SSE stream for POST /v1/responses (OpenAI Responses API).

        Emits spec-compliant event types as the agent runs:

        - ``response.created`` -- initial envelope (status=in_progress)
        - ``response.output_text.delta`` / ``response.output_text.done`` --
          streamed assistant text
        - ``response.output_item.added`` / ``response.output_item.done``
          with ``item.type == "function_call"`` -- when the agent invokes a
          tool (both events fire; the ``done`` event carries the finalized
          ``arguments`` string)
        - ``response.output_item.added`` with
          ``item.type == "function_call_output"`` -- tool result with
          ``{call_id, output, status}``
        - ``response.completed`` -- terminal event carrying the full
          response object with all output items + usage (same payload
          shape as the non-streaming path for parity)
        - ``response.failed`` -- terminal event on agent error

        If the client disconnects mid-stream, ``agent.interrupt()`` is
        called so the agent stops issuing upstream LLM calls, then the
        asyncio task is cancelled.  When ``store=True`` an initial
        ``in_progress`` snapshot is persisted immediately after
        ``response.created`` and disconnects update it to an
        ``incomplete`` snapshot so GET /v1/responses/{id} and
        ``previous_response_id`` chaining still have something to
        recover from.
        """
        import queue as _q

        sse_headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
        origin = request.headers.get("Origin", "")
        cors = self._cors_headers_for_origin(origin) if origin else None
        if cors:
            sse_headers.update(cors)
        if session_id:
            sse_headers["X-Encre-Session-Id"] = session_id
        if gateway_session_key:
            sse_headers["X-Encre-Session-Key"] = gateway_session_key
        response = web.StreamResponse(status=200, headers=sse_headers)
        await response.prepare(request)

        # State accumulated during the stream
        final_text_parts: List[str] = []
        # Track open function_call items by name so we can emit a matching
        # ``done`` event when the tool completes.  Order preserved.
        pending_tool_calls: List[Dict[str, Any]] = []
        # Output items we've emitted so far (used to build the terminal
        # response.completed payload).  Kept in the order they appeared.
        emitted_items: List[Dict[str, Any]] = []
        # Monotonic counter for output_index (spec requires it).
        output_index = 0
        # Monotonic counter for call_id generation if the agent doesn't
        # provide one (it doesn't, from tool_progress_callback).
        call_counter = 0
        # Canonical Responses SSE events include a monotonically increasing
        # sequence_number. Add it server-side for every emitted event so
        # clients that validate the OpenAI event schema can parse our stream.
        sequence_number = 0
        # Track the assistant message item id + content index for text
        # delta events -- the spec ties deltas to a specific item.
        message_item_id = f"msg_{uuid.uuid4().hex[:24]}"
        message_output_index: Optional[int] = None
        message_opened = False

        async def _write_event(event_type: str, data: Dict[str, Any]) -> None:
            """Write one SSE frame, assigning a sequence number if absent.

            The sequence number is only injected when the caller did not set
            one, so explicit values on special frames survive, while the
            monotonic counter still advances past them.
            """
            nonlocal sequence_number
            if "sequence_number" not in data:
                data["sequence_number"] = sequence_number
            sequence_number += 1
            payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            await response.write(payload.encode())

        def _envelope(status: str) -> Dict[str, Any]:
            """Build the base ``response`` envelope for a given status.

            Only the stable identification fields are included; callers add
            ``output`` and ``usage`` when those are known.
            """
            env: Dict[str, Any] = {
                "id": response_id,
                "object": "response",
                "status": status,
                "created_at": created_at,
                "model": model,
            }
            return env

        final_response_text = ""
        agent_error: Optional[str] = None
        usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        terminal_snapshot_persisted = False

        def _persist_response_snapshot(
            response_env: Dict[str, Any],
            *,
            conversation_history_snapshot: Optional[List[Dict[str, Any]]] = None,
        ) -> None:
            """Store a response snapshot and its conversation history.

            When *conversation_history_snapshot* is omitted it is derived from
            the in-flight history plus the current user message. Linking the
            conversation name to the response id is what makes
            ``conversation``-style lookups resolve later.
            """
            if not store:
                return
            if conversation_history_snapshot is None:
                conversation_history_snapshot = list(conversation_history)
                conversation_history_snapshot.append({"role": "user", "content": user_message})
            self._response_store.put(response_id, {
                "response": response_env,
                "conversation_history": conversation_history_snapshot,
                "instructions": instructions,
                "session_id": session_id,
            })
            if conversation:
                self._response_store.set_conversation(conversation, response_id)

        def _persist_incomplete_if_needed() -> None:
            """Persist an ``incomplete`` snapshot if no terminal one was written.

            Called from both the client-disconnect (``ConnectionResetError``)
            and server-cancellation (``asyncio.CancelledError``) paths so
            GET /v1/responses/{id} and ``previous_response_id`` chaining keep
            working after abrupt stream termination.
            """
            if not store or terminal_snapshot_persisted:
                return
            incomplete_text = "".join(final_text_parts) or final_response_text
            incomplete_items: List[Dict[str, Any]] = list(emitted_items)
            if incomplete_text:
                incomplete_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": incomplete_text}],
                })
            incomplete_env = _envelope("incomplete")
            incomplete_env["output"] = incomplete_items
            incomplete_env["usage"] = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
            incomplete_history = list(conversation_history)
            incomplete_history.append({"role": "user", "content": user_message})
            if incomplete_text:
                incomplete_history.append({"role": "assistant", "content": incomplete_text})
            _persist_response_snapshot(
                incomplete_env,
                conversation_history_snapshot=incomplete_history,
            )

        try:
            # response.created -- initial envelope, status=in_progress
            created_env = _envelope("in_progress")
            created_env["output"] = []
            await _write_event("response.created", {
                "type": "response.created",
                "response": created_env,
            })
            _persist_response_snapshot(created_env)
            last_activity = time.monotonic()

            async def _open_message_item() -> None:
                """Emit response.output_item.added for the assistant message
                the first time any text delta arrives."""
                nonlocal message_opened, message_output_index, output_index
                if message_opened:
                    return
                message_opened = True
                message_output_index = output_index
                output_index += 1
                item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                }
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": message_output_index,
                    "item": item,
                })

            async def _emit_text_delta(delta_text: str) -> None:
                """Append a text delta and emit it, opening the item first.

                The delta is also retained in ``final_text_parts`` so the
                final ``response.completed`` and any persisted incomplete
                snapshot carry the same text the client streamed.
                """
                await _open_message_item()
                final_text_parts.append(delta_text)
                await _write_event("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "delta": delta_text,
                    "logprobs": [],
                })

            async def _emit_tool_started(payload: Dict[str, Any]) -> str:
                """Emit response.output_item.added for a function_call.

                Returns the call_id so the matching completion event can
                reference it.  Prefer the real ``tool_call_id`` from the
                agent when available; fall back to a generated call id for
                safety in tests or older code paths.
                """
                nonlocal output_index, call_counter
                call_counter += 1
                call_id = payload.get("tool_call_id") or f"call_{response_id[5:]}_{call_counter}"
                args = payload.get("arguments", {})
                if isinstance(args, dict):
                    arguments_str = json.dumps(args)
                else:
                    arguments_str = str(args)
                item = {
                    "id": f"fc_{uuid.uuid4().hex[:24]}",
                    "type": "function_call",
                    "status": "in_progress",
                    "name": payload.get("name", ""),
                    "call_id": call_id,
                    "arguments": arguments_str,
                }
                idx = output_index
                output_index += 1
                pending_tool_calls.append({
                    "call_id": call_id,
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "item_id": item["id"],
                    "output_index": idx,
                })
                emitted_items.append({
                    "type": "function_call",
                    "name": payload.get("name", ""),
                    "arguments": arguments_str,
                    "call_id": call_id,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": item,
                })
                return call_id

            async def _emit_tool_completed(payload: Dict[str, Any]) -> None:
                """Emit response.output_item.done (function_call) followed
                by response.output_item.added (function_call_output)."""
                nonlocal output_index
                call_id = payload.get("tool_call_id")
                result = payload.get("result", "")
                pending = None
                if call_id:
                    for i, p in enumerate(pending_tool_calls):
                        if p["call_id"] == call_id:
                            pending = pending_tool_calls.pop(i)
                            break
                if pending is None:
                    # Completion without a matching start -- skip to avoid
                    # emitting orphaned done events.
                    return

                # function_call done
                done_item = {
                    "id": pending["item_id"],
                    "type": "function_call",
                    "status": "completed",
                    "name": pending["name"],
                    "call_id": pending["call_id"],
                    "arguments": pending["arguments"],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": pending["output_index"],
                    "item": done_item,
                })

                # function_call_output added (result)
                result_str = result if isinstance(result, str) else json.dumps(result)
                output_parts = [{"type": "input_text", "text": result_str}]
                output_item = {
                    "id": f"fco_{uuid.uuid4().hex[:24]}",
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                    "status": "completed",
                }
                idx = output_index
                output_index += 1
                emitted_items.append({
                    "type": "function_call_output",
                    "call_id": pending["call_id"],
                    "output": output_parts,
                })
                await _write_event("response.output_item.added", {
                    "type": "response.output_item.added",
                    "output_index": idx,
                    "item": output_item,
                })
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": idx,
                    "item": output_item,
                })

            # Main drain loop -- thread-safe queue fed by agent callbacks.
            async def _dispatch(it) -> None:
                """Route a queue item to the correct SSE emitter.

                Plain strings are text deltas -- they are batched (50ms)
                to reduce Open WebUI re-render storms.  Tagged tuples
                with ``__tool_started__`` / ``__tool_completed__``
                prefixes are tool lifecycle events and flush the buffer
                before emitting.
                """
                nonlocal _batch_timer
                if isinstance(it, tuple) and len(it) == 2 and isinstance(it[0], str):
                    tag, payload = it
                    # Flush batched text before tool events
                    if _batch_buf:
                        await _flush_batch()
                    if tag == "__tool_started__":
                        await _emit_tool_started(payload)
                    elif tag == "__tool_completed__":
                        await _emit_tool_completed(payload)
                elif isinstance(it, str):
                    # Batch text deltas -- append to buffer, flush on timer
                    _batch_buf.append(it)
                    if _batch_timer is None:
                        _batch_timer = asyncio.create_task(_batch_flush_after(0.05))
                # Other types are silently dropped.

            # -- Batching state --
            _batch_buf: List[str] = []
            _batch_timer: Optional[asyncio.Task] = None
            _batch_lock = asyncio.Lock()

            async def _batch_flush_after(delay: float) -> None:
                """Wait delay seconds, then flush accumulated text deltas."""
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                # Clear timer reference BEFORE flush so new deltas
                # can start a fresh timer while we emit
                nonlocal _batch_buf, _batch_timer
                _batch_timer = None
                await _flush_batch()

            async def _flush_batch() -> None:
                """Emit a single SSE delta for all accumulated text."""
                nonlocal _batch_buf
                async with _batch_lock:
                    if _batch_buf:
                        combined = "".join(_batch_buf)
                        _batch_buf = []
                        await _emit_text_delta(combined)

            loop = asyncio.get_running_loop()
            while True:
                try:
                    item = await loop.run_in_executor(None, lambda: stream_q.get(timeout=0.5))
                except _q.Empty:
                    if agent_task.done():
                        # Drain remaining
                        while True:
                            try:
                                item = stream_q.get_nowait()
                                if item is None:
                                    break
                                await _dispatch(item)
                                last_activity = time.monotonic()
                            except _q.Empty:
                                break
                        break
                    if time.monotonic() - last_activity >= CHAT_COMPLETIONS_SSE_KEEPALIVE_SECONDS:
                        await response.write(b": keepalive\n\n")
                        last_activity = time.monotonic()
                    continue

                if item is None:  # EOS sentinel
                    # Cancel pending timer and flush remaining batched text
                    if _batch_timer and not _batch_timer.done():
                        _batch_timer.cancel()
                        _batch_timer = None
                    if _batch_buf:
                        await _flush_batch()
                    break

                await _dispatch(item)
                last_activity = time.monotonic()

            # Flush any final batched text before processing result
            if _batch_buf:
                await _flush_batch()

            # Pick up agent result + usage from the completed task
            try:
                result, agent_usage = await agent_task
                usage = agent_usage or usage
                # If the agent produced a final_response but no text
                # deltas were streamed (e.g. some providers only emit
                # the full response at the end), emit a single fallback
                # delta so Responses clients still receive a live text part.
                agent_final = result.get("final_response", "") if isinstance(result, dict) else ""
                if agent_final and not final_text_parts:
                    await _emit_text_delta(agent_final)
                if agent_final and not final_response_text:
                    final_response_text = agent_final
                if isinstance(result, dict) and result.get("error") and not final_response_text:
                    agent_error = _redact_api_error_text(result["error"])
            except Exception as e:  # noqa: BLE001
                logger.error("Error running agent for streaming responses: %s", e, exc_info=True)
                agent_error = _redact_api_error_text(e)

            # Close the message item if it was opened
            final_response_text = "".join(final_text_parts) or final_response_text
            if message_opened:
                await _write_event("response.output_text.done", {
                    "type": "response.output_text.done",
                    "item_id": message_item_id,
                    "output_index": message_output_index,
                    "content_index": 0,
                    "text": final_response_text,
                    "logprobs": [],
                })
                msg_done_item = {
                    "id": message_item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": final_response_text}
                    ],
                }
                await _write_event("response.output_item.done", {
                    "type": "response.output_item.done",
                    "output_index": message_output_index,
                    "item": msg_done_item,
                })

            # Always append a final message item in the completed
            # response envelope so clients that only parse the terminal
            # payload still see the assistant text.  This mirrors the
            # shape produced by _extract_output_items in the batch path.
            final_items: List[Dict[str, Any]] = list(emitted_items)

            # Trim large content from tool call arguments to keep the
            # response.completed event under ~100KB.  Clients already
            # received full details via incremental events.
            for _item in final_items:
                if _item.get("type") == "function_call":
                    try:
                        _args = json.loads(_item.get("arguments", "{}")) if isinstance(_item.get("arguments"), str) else _item.get("arguments", {})
                        if isinstance(_args, dict):
                            for _k in ("content", "query", "pattern", "old_string", "new_string"):
                                if isinstance(_args.get(_k), str) and len(_args[_k]) > 500:
                                    _args[_k] = "[" + str(len(_args[_k])) + " chars 鈥?truncated for response.completed]"
                            _item["arguments"] = json.dumps(_args)
                    except Exception:
                        pass
                elif _item.get("type") == "function_call_output":
                    _output = _item.get("output", [])
                    if isinstance(_output, list) and _output:
                        _first = _output[0]
                        if isinstance(_first, dict) and _first.get("type") == "input_text":
                            _text = _first.get("text", "")
                            if len(_text) > 1000:
                                _first["text"] = _text[:500] + "...[" + str(len(_text) - 500) + " more chars]"
                                _item["output"] = [_first]

            final_items.append({
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": final_response_text or (_redact_api_error_text(agent_error) if agent_error else "")}
                ],
            })

            if agent_error:
                failed_env = _envelope("failed")
                failed_env["output"] = final_items
                failed_env["error"] = {"message": _redact_api_error_text(agent_error), "type": "server_error"}
                failed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                _failed_history = list(conversation_history)
                _failed_history.append({"role": "user", "content": user_message})
                if final_response_text or agent_error:
                    _failed_history.append({
                        "role": "assistant",
                        "content": final_response_text or _redact_api_error_text(agent_error),
                    })
                _persist_response_snapshot(
                    failed_env,
                    conversation_history_snapshot=_failed_history,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.failed", {
                    "type": "response.failed",
                    "response": failed_env,
                })
            else:
                completed_env = _envelope("completed")
                completed_env["output"] = final_items
                completed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                full_history = self._build_response_conversation_history(
                    conversation_history,
                    user_message,
                    result,
                    final_response_text,
                )
                _persist_response_snapshot(
                    completed_env,
                    conversation_history_snapshot=full_history,
                )
                terminal_snapshot_persisted = True
                await _write_event("response.completed", {
                    "type": "response.completed",
                    "response": completed_env,
                })

        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
            _persist_incomplete_if_needed()
            # Client disconnected -- interrupt the agent so it stops
            # making upstream LLM calls, then cancel the task.
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
                try:
                    await agent_task
                except (asyncio.CancelledError, Exception):
                    pass
            logger.info("SSE client disconnected; interrupted agent task %s", response_id)
        except asyncio.CancelledError:
            # Server-side cancellation (e.g. shutdown, request timeout) --
            # persist an incomplete snapshot so GET /v1/responses/{id} and
            # previous_response_id chaining still work, then re-raise so the
            # runtime's cancellation semantics are respected.
            _persist_incomplete_if_needed()
            agent = agent_ref[0] if agent_ref else None
            if agent is not None:
                try:
                    agent.interrupt("SSE task cancelled")
                except Exception:
                    pass
            if not agent_task.done():
                agent_task.cancel()
            logger.info("SSE task cancelled; persisted incomplete snapshot for %s", response_id)
            raise
        except Exception as _exc:
            # Agent crashed with an unhandled error (e.g. model API error like
            # BadRequestError, AuthenticationError).  Emit a response.failed
            # event and properly terminate the SSE stream so the client doesn't
            # get a TransferEncodingError from incomplete chunked encoding.
            import traceback as _tb
            _persist_incomplete_if_needed()
            agent_error = _redact_api_error_text(_tb.format_exc())
            try:
                failed_env = _envelope("failed")
                failed_env["output"] = list(emitted_items)
                failed_env["error"] = {"message": _redact_api_error_text(_exc, limit=500), "type": "server_error"}
                failed_env["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
                await _write_event("response.failed", {
                    "type": "response.failed",
                    "response": failed_env,
                })
            except Exception:
                pass
            logger.error("Agent crashed mid-stream for %s: %s", response_id, str(agent_error)[:300])

        return response

    @_admit_api_agent_request
    async def _handle_responses(self, request: "web.Request") -> "web.Response":
        """POST /v1/responses -- OpenAI Responses API format."""
        # Bound total in-flight agent runs (configurable; #7483).
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited

        # Long-term memory scope header (see chat_completions for details).
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        # Parse request body
        try:
            body = await request.json()
        except (json.JSONDecodeError, Exception):
            return web.json_response(
                {"error": {"message": "Invalid JSON in request body", "type": "invalid_request_error"}},
                status=400,
            )

        raw_input = body.get("input")
        if raw_input is None:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")
        conversation = body.get("conversation")
        store = _coerce_request_bool(body.get("store"), default=True)

        # conversation and previous_response_id are mutually exclusive
        if conversation and previous_response_id:
            return web.json_response(_openai_error("Cannot use both 'conversation' and 'previous_response_id'"), status=400)

        # Resolve conversation name to latest response_id
        if conversation:
            previous_response_id = self._response_store.get_conversation(conversation)
            # No error if conversation doesn't exist yet -- it's a new conversation

        # Normalize input to message list
        input_messages: List[Dict[str, Any]] = []
        if isinstance(raw_input, str):
            input_messages = [{"role": "user", "content": raw_input}]
        elif isinstance(raw_input, list):
            for idx, item in enumerate(raw_input):
                if isinstance(item, str):
                    input_messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    role = item.get("role", "user")
                    try:
                        content = _normalize_multimodal_content(item.get("content", ""))
                    except ValueError as exc:
                        return _multimodal_validation_error(exc, param=f"input[{idx}].content")
                    input_messages.append({"role": role, "content": content})
        else:
            return web.json_response(_openai_error("'input' must be a string or array"), status=400)

        # Accept explicit conversation_history from the request body.
        # This lets stateless clients supply their own history instead of
        # relying on server-side response chaining via previous_response_id.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, Any]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                try:
                    entry_content = _normalize_multimodal_content(entry["content"])
                except ValueError as exc:
                    return _multimodal_validation_error(exc, param=f"conversation_history[{i}].content")
                conversation_history.append({"role": str(entry["role"]), "content": entry_content})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored is None:
                return web.json_response(_openai_error(f"Previous response not found: {previous_response_id}"), status=404)
            conversation_history = list(stored.get("conversation_history", []))
            stored_session_id = stored.get("session_id")
            # If no instructions provided, carry forward from previous
            if instructions is None:
                instructions = stored.get("instructions")

        # Append new input messages to history (all but the last become history)
        for msg in input_messages[:-1]:
            conversation_history.append(msg)

        # Last input message is the user_message
        user_message: Any = input_messages[-1].get("content", "") if input_messages else ""
        if not _content_has_visible_payload(user_message):
            return web.json_response(_openai_error("No user message found in input"), status=400)

        # Truncation support
        if body.get("truncation") == "auto" and len(conversation_history) > 100:
            conversation_history = conversation_history[-100:]

        # Reuse session from previous_response_id chain so the dashboard
        # groups the entire conversation under one session entry.
        session_id = stored_session_id or str(uuid.uuid4())

        # Per-client model routing for /v1/responses (see model_routes).
        route = self._resolve_route(body.get("model"))

        stream = _coerce_request_bool(body.get("stream"), default=False)
        if stream:
            # Streaming branch -- emit OpenAI Responses SSE events as the
            # agent runs so frontends can render text deltas and tool
            # calls in real time.  See _write_sse_responses for details.
            import queue as _q
            _stream_q: _q.Queue = _q.Queue()

            def _on_delta(delta):
                """Queue a text delta; drop the agent's ``None`` signal."""
                # None from the agent is a CLI box-close signal, not EOS.
                # Forwarding would kill the SSE stream prematurely; the
                # SSE writer detects completion via agent_task.done().
                if delta is not None:
                    _stream_q.put(delta)

            def _on_tool_progress(event_type, name, preview, args, **kwargs):
                """Queue non-start tool progress events if needed in future.

                The structured Responses stream uses ``tool_start_callback``
                and ``tool_complete_callback`` for exact call-id correlation,
                so progress events are currently ignored here.
                """
                return

            def _on_tool_start(tool_call_id, function_name, function_args):
                """Queue a started tool for live function_call streaming."""
                _stream_q.put(("__tool_started__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                }))

            def _on_tool_complete(tool_call_id, function_name, function_args, function_result):
                """Queue a completed tool result for live function_call_output streaming."""
                _stream_q.put(("__tool_completed__", {
                    "tool_call_id": tool_call_id,
                    "name": function_name,
                    "arguments": function_args or {},
                    "result": function_result,
                }))

            agent_ref = [None]
            agent_task = asyncio.ensure_future(self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool_progress,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                agent_ref=agent_ref,
                gateway_session_key=gateway_session_key,
                route=route,
            ))
            # Ensure SSE drain loops can terminate without relying on polling
            # agent_task.done(), which can race with queue timeout checks.
            agent_task.add_done_callback(lambda _fut: _stream_q.put(None))

            response_id = f"resp_{uuid.uuid4().hex[:28]}"
            model_name = body.get("model", self._model_name)
            created_at = int(time.time())

            return await self._write_sse_responses(
                request=request,
                response_id=response_id,
                model=model_name,
                created_at=created_at,
                stream_q=_stream_q,
                agent_task=agent_task,
                agent_ref=agent_ref,
                conversation_history=conversation_history,
                user_message=user_message,
                instructions=instructions,
                conversation=conversation,
                store=store,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
            )

        async def _compute_response():
            """Run the non-streaming agent turn for the Responses API."""
            return await self._run_agent(
                user_message=user_message,
                conversation_history=conversation_history,
                ephemeral_system_prompt=instructions,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                route=route,
            )

        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key:
            fp = _make_request_fingerprint(
                body,
                keys=["input", "instructions", "previous_response_id", "conversation", "model", "tools"],
            )
            try:
                result, usage = await _idem_cache.get_or_set(idempotency_key, fp, _compute_response)
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )
        else:
            try:
                result, usage = await _compute_response()
            except Exception as e:
                logger.error("Error running agent for responses: %s", e, exc_info=True)
                return web.json_response(
                    _openai_error(f"Internal server error: {e}", err_type="server_error"),
                    status=500,
                )

        final_response = _resolve_media_to_data_urls(result.get("final_response", ""))
        if not final_response:
            final_response = _redact_api_error_text(result.get("error", "(No response generated)"))

        response_id = f"resp_{uuid.uuid4().hex[:28]}"
        created_at = int(time.time())

        # Build the full conversation history for storage
        # (includes tool calls from the agent run)
        full_history = self._build_response_conversation_history(
            conversation_history,
            user_message,
            result,
            final_response,
        )

        # Build output items from the current turn only.  AIAgent returns a
        # full transcript in result["messages"], while older/mocked paths may
        # return only the current turn suffix.
        output_start_index = self._response_messages_turn_start_index(
            conversation_history,
            user_message,
            result,
        )
        output_items = self._extract_output_items(result, start_index=output_start_index)

        response_data = {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "created_at": created_at,
            "model": body.get("model", self._model_name),
            "output": output_items,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            },
        }

        # Store the complete response object for future chaining / GET retrieval
        if store:
            self._response_store.put(response_id, {
                "response": response_data,
                "conversation_history": full_history,
                "instructions": instructions,
                "session_id": session_id,
            })
            # Update conversation mapping so the next request with the same
            # conversation name automatically chains to this response
            if conversation:
                self._response_store.set_conversation(conversation, response_id)

        response_headers = {"X-Encre-Session-Id": session_id}
        if gateway_session_key:
            response_headers["X-Encre-Session-Key"] = gateway_session_key
        return web.json_response(response_data, headers=response_headers)

    # ------------------------------------------------------------------
    # GET / DELETE response endpoints
    # ------------------------------------------------------------------

    async def _handle_get_response(self, request: "web.Request") -> "web.Response":
        """GET /v1/responses/{response_id} -- retrieve a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        stored = self._response_store.get(response_id)
        if stored is None:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response(stored["response"])

    async def _handle_delete_response(self, request: "web.Request") -> "web.Response":
        """DELETE /v1/responses/{response_id} -- delete a stored response."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        response_id = request.match_info["response_id"]
        deleted = self._response_store.delete(response_id)
        if not deleted:
            return web.json_response(_openai_error(f"Response not found: {response_id}"), status=404)

        return web.json_response({
            "id": response_id,
            "object": "response",
            "deleted": True,
        })

    # ------------------------------------------------------------------
    # Cron jobs API
    # ------------------------------------------------------------------

    _JOB_ID_RE = __import__("re").compile(r"[a-f0-9]{12}")
    # Allowed fields for update -- prevents clients injecting arbitrary keys
    _UPDATE_ALLOWED_FIELDS = {"name", "schedule", "prompt", "deliver", "skills", "skill", "repeat", "enabled"}
    _MAX_NAME_LENGTH = 200
    _MAX_PROMPT_LENGTH = 5000

    @staticmethod
    def _check_jobs_available() -> Optional["web.Response"]:
        """Return error response if cron module isn't available."""
        if not _CRON_AVAILABLE:
            return web.json_response(
                {"error": "Cron module not available"}, status=501,
            )
        return None

    def _check_job_id(self, request: "web.Request") -> tuple:
        """Validate and extract job_id. Returns (job_id, error_response)."""
        job_id = request.match_info["job_id"]
        if not self._JOB_ID_RE.fullmatch(job_id):
            logger.warning(
                "Cron jobs API rejected invalid job_id %r: %s",
                job_id,
                self._request_audit_log_suffix(request),
            )
            return job_id, web.json_response(
                {"error": "Invalid job ID format"}, status=400,
            )
        return job_id, None

    async def _handle_list_jobs(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs -- list all cron jobs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            include_disabled = request.query.get("include_disabled", "").lower() in {"true", "1"}
            jobs = _cron_list(include_disabled=include_disabled)
            return web.json_response({"jobs": jobs})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_create_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs -- create a new cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            schedule = (body.get("schedule") or "").strip()
            prompt = body.get("prompt", "")
            deliver = body.get("deliver", "local")
            skills = body.get("skills")
            repeat = body.get("repeat")

            if not name:
                return web.json_response({"error": "Name is required"}, status=400)
            if len(name) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be 鈮?{self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if not schedule:
                return web.json_response({"error": "Schedule is required"}, status=400)
            if len(prompt) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be 鈮?{self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if prompt and _scan_cron_prompt is not None:
                scan_error = _scan_cron_prompt(prompt)
                if scan_error:
                    return web.json_response({"error": scan_error}, status=400)
            if repeat is not None and (not isinstance(repeat, int) or repeat < 1):
                return web.json_response({"error": "Repeat must be a positive integer"}, status=400)

            kwargs = {
                "prompt": prompt,
                "schedule": schedule,
                "name": name,
                "deliver": deliver,
                "origin": self._cron_origin_from_request(request),
            }
            if skills:
                kwargs["skills"] = skills
            if repeat is not None:
                kwargs["repeat"] = repeat

            job = _cron_create(**kwargs)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_get_job(self, request: "web.Request") -> "web.Response":
        """GET /api/jobs/{job_id} -- get a single cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_get(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_update_job(self, request: "web.Request") -> "web.Response":
        """PATCH /api/jobs/{job_id} -- update a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            body = await request.json()
            # Whitelist allowed fields to prevent arbitrary key injection
            sanitized = {k: v for k, v in body.items() if k in self._UPDATE_ALLOWED_FIELDS}
            if not sanitized:
                return web.json_response({"error": "No valid fields to update"}, status=400)
            # Validate lengths if present
            if "name" in sanitized and len(sanitized["name"]) > self._MAX_NAME_LENGTH:
                return web.json_response(
                    {"error": f"Name must be 鈮?{self._MAX_NAME_LENGTH} characters"}, status=400,
                )
            if "prompt" in sanitized and len(sanitized["prompt"]) > self._MAX_PROMPT_LENGTH:
                return web.json_response(
                    {"error": f"Prompt must be 鈮?{self._MAX_PROMPT_LENGTH} characters"}, status=400,
                )
            if sanitized.get("prompt") and _scan_cron_prompt is not None:
                scan_error = _scan_cron_prompt(sanitized["prompt"])
                if scan_error:
                    return web.json_response({"error": scan_error}, status=400)
            job = _cron_update(job_id, sanitized)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_delete_job(self, request: "web.Request") -> "web.Response":
        """DELETE /api/jobs/{job_id} -- delete a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            success = _cron_remove(job_id)
            if not success:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_pause_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/pause -- pause a cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_pause(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_resume_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/resume -- resume a paused cron job."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_resume(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            _notify_cron_provider_jobs_changed()
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_run_job(self, request: "web.Request") -> "web.Response":
        """POST /api/jobs/{job_id}/run -- trigger immediate execution."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err
        draining = self._draining_response()
        if draining is not None:
            return draining
        cron_err = self._check_jobs_available()
        if cron_err:
            return cron_err
        job_id, id_err = self._check_job_id(request)
        if id_err:
            return id_err
        try:
            job = _cron_trigger(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response({"job": job})
        except Exception as e:
            return web.json_response({"error": _redact_api_error_text(e)}, status=500)

    async def _handle_cron_fire(self, request: "web.Request") -> "web.Response":
        """POST /api/cron/fire -- Chronos managed-cron fire webhook (NAS -> agent).

        Authenticated by a NAS-minted JWT (verified via the pluggable
        fire-verifier), NOT API_SERVER_KEY -- NAS holds no API server key, and
        this is the only inbound that can trigger remote job execution, so it
        gets its own purpose-scoped token check.

        Returns 202 + runs the job in the background so a long agent turn never
        trips NAS's HTTP timeout. The store CAS claim inside fire_due guards
        against double-fire on a NAS/scheduler retry.
        """
        from encre.config import cfg_get, load_config
        from plugins.cron_providers.chronos.verify import get_fire_verifier

        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""

        cfg = load_config()
        claims = get_fire_verifier()(
            token=token,
            expected_audience=cfg_get(cfg, "cron", "chronos", "expected_audience", default=""),
            jwks_or_key=cfg_get(cfg, "cron", "chronos", "nas_jwks_url", default="") or None,
            issuer=cfg_get(cfg, "cron", "chronos", "portal_url", default="") or None,
        )
        if claims is None:
            logger.warning(
                "cron fire: rejected invalid token: %s",
                self._request_audit_log_suffix(request),
            )
            return web.json_response({"error": "invalid fire token"}, status=401)
        draining = self._draining_response()
        if draining is not None:
            return draining

        with _reserve_pending_api_work(self) as reservation:
            try:
                body = await request.json()
            except Exception:
                body = {}
            job_id = (body or {}).get("job_id")
            if not job_id:
                return web.json_response({"error": "missing job_id"}, status=400)

            from cron.scheduler_provider import resolve_cron_scheduler
            provider = resolve_cron_scheduler()

            loop = asyncio.get_running_loop()
            # Fire in the background (202 immediately). fire_due claims via the
            # store CAS, so a retry while this is in flight is de-duped.
            task = asyncio.create_task(
                asyncio.to_thread(provider.fire_due, job_id, adapters=None, loop=loop)
            )
            reservation["detached"] = True
            task.add_done_callback(
                lambda _task: _release_pending_api_work(self, reservation)
            )
            try:
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except (TypeError, AttributeError):
                pass

            return web.json_response({"status": "accepted", "job_id": job_id}, status=202)


    # ------------------------------------------------------------------
    # Output extraction helper
    # ------------------------------------------------------------------

    @staticmethod
    def _build_response_conversation_history(
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
        final_response: Any,
    ) -> List[Dict[str, Any]]:
        """Build the stored Responses transcript without duplicating history."""
        prior = list(conversation_history)
        current_user = {"role": "user", "content": user_message}
        agent_messages = result.get("messages") if isinstance(result, dict) else None

        if isinstance(agent_messages, list) and agent_messages:
            turn_start = APIServerAdapter._response_messages_turn_start_index(
                conversation_history,
                user_message,
                result,
            )
            if turn_start:
                return list(agent_messages)

            full_history = prior
            full_history.append(current_user)
            full_history.extend(agent_messages)
            return full_history

        full_history = prior
        full_history.append(current_user)
        full_history.append({"role": "assistant", "content": final_response})
        return full_history

    @staticmethod
    def _response_messages_turn_start_index(
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
    ) -> int:
        """Detect transcript-shaped result["messages"] and return turn start."""
        agent_messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(agent_messages, list) or not agent_messages:
            return 0

        prior = list(conversation_history)
        current_user = {"role": "user", "content": user_message}
        expected_prefix = prior + [current_user]
        if agent_messages[:len(expected_prefix)] == expected_prefix:
            return len(expected_prefix)
        if prior and agent_messages[:len(prior)] == prior:
            return len(prior)
        return 0

    @classmethod
    def _turn_transcript_messages(
        cls,
        conversation_history: List[Dict[str, Any]],
        user_message: Any,
        result: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Return this turn's assistant/tool messages in client-safe shape.

        The streaming SSE contract delivers all assistant text as
        ``assistant.delta`` events under one ``message_id`` interleaved with
        ``tool.*`` events, and a single ``assistant.completed`` carrying only
        the final reply.  A client that accumulates deltas into one buffer
        cannot reconstruct *intermediate* assistant text segments that preceded
        tool calls -- so when the page is re-opened mid/post-stream those
        segments appear lost, even though state.db persisted them correctly.

        Emitting the authoritative per-turn transcript on ``run.completed`` lets
        any SSE consumer reconcile its live view against ground truth without a
        separate ``GET /messages`` round-trip.  Purely additive: clients that
        ignore the field are unaffected.  Refs #34703.
        """
        agent_messages = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(agent_messages, list) or not agent_messages:
            return []
        start = cls._response_messages_turn_start_index(
            conversation_history, user_message, result
        )
        turn = agent_messages[start:]
        out: List[Dict[str, Any]] = []
        for msg in turn:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") not in {"assistant", "tool"}:
                continue
            out.append(cls._message_response(msg))
        return out

    @staticmethod
    def _extract_output_items(result: Dict[str, Any], start_index: int = 0) -> List[Dict[str, Any]]:
        """
        Build the output item array from the agent's messages.

        Walks *result["messages"]* starting at *start_index* and emits:
        - ``function_call`` items for each tool_call on assistant messages
        - ``function_call_output`` items for each tool-role message
        - a final ``message`` item with the assistant's text reply
        """
        items: List[Dict[str, Any]] = []
        messages = result.get("messages", [])
        if start_index > 0:
            messages = messages[start_index:]

        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    items.append({
                        "type": "function_call",
                        "name": func.get("name", ""),
                        "arguments": func.get("arguments", ""),
                        "call_id": tc.get("id", ""),
                    })
            elif role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": msg.get("content", ""),
                })

        # Final assistant message
        final = result.get("final_response", "")
        if not final:
            final = _redact_api_error_text(result.get("error", "(No response generated)"))

        items.append({
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": final,
                }
            ],
        })
        return items

    # ------------------------------------------------------------------
    # Agent execution
    # ------------------------------------------------------------------

    def _concurrency_limited_response(self) -> Optional["web.Response"]:
        """Return a 429 response if the concurrent-run cap is reached, else None.

        The cap bounds total in-flight agent activity across every
        agent-serving endpoint. Reuse the same adapter-owned work count that
        shutdown draining uses, including an admitted request before it reaches
        agent/task bookkeeping. Stream queues are transport state and may
        disappear while their underlying run remains active, so they must not
        define run concurrency. A configured value of 0 disables the cap.
        """
        limit = self._max_concurrent_runs
        if limit <= 0:
            return None
        inflight = self.active_agent_work_count()
        # The current request owns one reservation until it hands off to
        # _run_agent() or /v1/runs task registration. It must not consume its
        # own last available slot; other admitted requests remain counted.
        reservation = _api_agent_request_reservation.get()
        if reservation and reservation["active"]:
            inflight -= 1
        if inflight >= limit:
            return web.json_response(
                _openai_error(
                    f"Too many concurrent runs (max {limit})",
                    err_type="rate_limit_error",
                    code="rate_limit_exceeded",
                ),
                status=429,
                headers={"Retry-After": "1"},
            )
        return None

    @staticmethod
    def _bind_api_server_session(
        *,
        chat_id: str = "",
        session_key: str = "",
        session_id: str = "",
    ) -> list:
        """Bind session contextvars for an API-server agent run.

        This is the SINGLE structural chokepoint every API-server agent-entry
        path must use to seed session context -- it hardwires
        ``platform="api_server"`` and ``async_delivery=False`` so a new route
        physically cannot reintroduce the silent-no-op bug (#10760) by
        forgetting to mark the channel as non-delivering. There is no
        ``async_delivery`` parameter to get wrong; the stateless HTTP path can
        never wake the agent after the turn ends, on ANY route.

        Returns reset tokens; pass them to ``clear_session_vars`` in a
        ``finally`` block (the binding is request-scoped and must not outlive
        the turn -- a session resumed later on a delivering interface, e.g. the
        CLI or a gateway platform, re-binds fresh and is NOT blocked).
        """
        from encre.gateway.session_context import set_session_vars

        return set_session_vars(
            platform="api_server",
            chat_id=chat_id,
            session_key=session_key,
            session_id=session_id,
            async_delivery=False,
        )

    async def _run_agent(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        ephemeral_system_prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        stream_delta_callback=None,
        tool_progress_callback=None,
        tool_start_callback=None,
        tool_complete_callback=None,
        agent_ref: Optional[list] = None,
        gateway_session_key: Optional[str] = None,
        route: Optional[Dict[str, Any]] = None,
    ) -> tuple:
        """
        Create an agent and run a conversation in a thread executor.

        Returns ``(result_dict, usage_dict)`` where *usage_dict* contains
        ``input_tokens``, ``output_tokens`` and ``total_tokens``.

        *route* is an optional ``model_routes`` entry (resolved from the
        request's ``model`` field) that overrides the global model/provider
        for this specific request.

        If *agent_ref* is a one-element list, the AIAgent instance is stored
        at ``agent_ref[0]`` before ``run_conversation`` begins.  This allows
        callers (e.g. the SSE writer) to call ``agent.interrupt()`` from
        another thread to stop in-progress LLM calls.
        """
        loop = asyncio.get_running_loop()
        # Capture before hopping to the executor -- ContextVars do not follow
        # run_in_executor threads, so the profile scope must be re-entered
        # inside _run() from this explicit value.
        request_profile = _api_request_profile.get()

        def _run():
            """Build the agent and run one conversation turn in the executor.

            Runs in a worker thread, so the profile scope and session context
            vars are re-established here from values captured on the event
            loop: ``ContextVar`` does not propagate into ``run_in_executor``
            threads. The session tokens are always cleared in ``finally``.

            Returns:
                A ``(result, usage)`` tuple; ``result`` carries
                ``session_id`` when the agent rotated sessions mid-turn.
            """
            from encre.gateway.session_context import clear_session_vars

            with self._profile_scope(request_profile):
                tokens = self._bind_api_server_session(
                    chat_id=session_id or "",
                    session_key=gateway_session_key or session_id or "",
                    session_id=session_id or "",
                )
                try:
                    agent = self._create_agent(
                        ephemeral_system_prompt=ephemeral_system_prompt,
                        session_id=session_id,
                        stream_delta_callback=stream_delta_callback,
                        tool_progress_callback=tool_progress_callback,
                        tool_start_callback=tool_start_callback,
                        tool_complete_callback=tool_complete_callback,
                        gateway_session_key=gateway_session_key,
                        route=route,
                    )
                    if agent_ref is not None:
                        agent_ref[0] = agent
                    effective_task_id = session_id or str(uuid.uuid4())
                    result = agent.run_conversation(
                        user_message=user_message,
                        conversation_history=conversation_history,
                        task_id=effective_task_id,
                    )
                    usage = {
                        "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                        "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                        "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                    }
                    # Include the effective session ID in the result so callers
                    # (e.g. X-Encre-Session-Id header) can track compression-
                    # triggered session rotations. (#16938)
                    _eff_sid = getattr(agent, "session_id", session_id)
                    if isinstance(_eff_sid, str) and _eff_sid:
                        result["session_id"] = _eff_sid
                    return result, usage
                finally:
                    clear_session_vars(tokens)

        self._activate_admitted_request()
        self._inflight_agent_runs += 1
        try:
            return await loop.run_in_executor(None, _run)
        finally:
            self._inflight_agent_runs -= 1

    # ------------------------------------------------------------------
    # /v1/runs -- structured event streaming
    # ------------------------------------------------------------------

    _RUN_STREAM_TTL = 300  # seconds before orphaned runs are swept
    _RUN_STATUS_TTL = 3600  # seconds to retain terminal run status for polling

    def _set_run_status(self, run_id: str, status: str, **fields: Any) -> Dict[str, Any]:
        """Update pollable run status without exposing private agent objects."""
        now = time.time()
        current = self._run_statuses.get(run_id, {})
        current.update({
            "object": "encre.run",
            "run_id": run_id,
            "status": status,
            "updated_at": now,
        })
        current.setdefault("created_at", fields.pop("created_at", now))
        current.update(fields)
        self._run_statuses[run_id] = current
        return current

    def _make_run_event_callback(self, run_id: str, loop: "asyncio.AbstractEventLoop"):
        """Return a tool_progress_callback that pushes structured events to the run's SSE queue."""
        def _push(event: Dict[str, Any]) -> None:
            """Record the last event and push it to the run's SSE queue.

            The status bookkeeping happens before the queue lookup so a
            consumer that has already disconnected still leaves the run's
            polling status accurate.
            """
            self._set_run_status(
                run_id,
                self._run_statuses.get(run_id, {}).get("status", "running"),
                last_event=event.get("event"),
            )
            q = self._run_streams.get(run_id)
            if q is None:
                return
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:
                pass

        def _callback(event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs):
            """Translate an agent progress event into a run event dict.

            Each agent event type maps to a distinct wire event carrying the
            run id and a timestamp so a consumer can order them against the
            run lifecycle it is polling.
            """
            ts = time.time()
            if event_type == "tool.started":
                _push({
                    "event": "tool.started",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "preview": preview,
                })
            elif event_type == "tool.completed":
                _push({
                    "event": "tool.completed",
                    "run_id": run_id,
                    "timestamp": ts,
                    "tool": tool_name,
                    "duration": round(kwargs.get("duration", 0), 3),
                    "error": kwargs.get("is_error", False),
                })
            elif event_type == "reasoning.available":
                _push({
                    "event": "reasoning.available",
                    "run_id": run_id,
                    "timestamp": ts,
                    "text": preview or "",
                })
            # _thinking and subagent_progress are intentionally not forwarded

        return _callback

    @_admit_api_agent_request
    async def _handle_runs(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs -- start an agent run, return run_id immediately."""
        # Long-term memory scope header (see chat_completions for details).
        gateway_session_key, key_err = self._parse_session_key_header(request)
        if key_err is not None:
            return key_err

        # Enforce concurrency limit (shared across all agent-serving
        # endpoints; configurable via encre.gateway.api_server.max_concurrent_runs).
        limited = self._concurrency_limited_response()
        if limited is not None:
            return limited

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_input = body.get("input")
        if not raw_input:
            return web.json_response(_openai_error("Missing 'input' field"), status=400)

        user_message = raw_input if isinstance(raw_input, str) else (raw_input[-1].get("content", "") if isinstance(raw_input, list) else "")
        if not user_message:
            return web.json_response(_openai_error("No user message found in input"), status=400)

        instructions = body.get("instructions")
        previous_response_id = body.get("previous_response_id")

        # Accept explicit conversation_history from the request body.
        # Precedence: explicit conversation_history > previous_response_id.
        conversation_history: List[Dict[str, str]] = []
        raw_history = body.get("conversation_history")
        if raw_history:
            if not isinstance(raw_history, list):
                return web.json_response(
                    _openai_error("'conversation_history' must be an array of message objects"),
                    status=400,
                )
            for i, entry in enumerate(raw_history):
                if not isinstance(entry, dict) or "role" not in entry or "content" not in entry:
                    return web.json_response(
                        _openai_error(f"conversation_history[{i}] must have 'role' and 'content' fields"),
                        status=400,
                    )
                conversation_history.append({"role": str(entry["role"]), "content": str(entry["content"])})
            if previous_response_id:
                logger.debug("Both conversation_history and previous_response_id provided; using conversation_history")

        stored_session_id = None
        if not conversation_history and previous_response_id:
            stored = self._response_store.get(previous_response_id)
            if stored:
                conversation_history = list(stored.get("conversation_history", []))
                stored_session_id = stored.get("session_id")
                if instructions is None:
                    instructions = stored.get("instructions")

        # When input is a multi-message array, extract all but the last
        # message as conversation history (the last becomes user_message).
        # Only fires when no explicit history was provided.
        if not conversation_history and isinstance(raw_input, list) and len(raw_input) > 1:
            for msg in raw_input[:-1]:
                if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
                    content = msg["content"]
                    if isinstance(content, list):
                        # Flatten multi-part content blocks to text
                        content = " ".join(
                            part.get("text", "") for part in content
                            if isinstance(part, dict) and part.get("type") == "text"
                        )
                    conversation_history.append({"role": msg["role"], "content": str(content)})

        run_id = f"run_{uuid.uuid4().hex}"
        session_id = body.get("session_id") or stored_session_id or run_id
        # Approval queues gate host-side tool execution and must be isolated
        # per API run.  Client-provided session IDs and memory session keys are
        # conversation/memory scopes, not authorization namespaces: multiple
        # concurrent runs can intentionally share them, and resolving an
        # approval for one run must not unblock another run's dangerous command.
        approval_session_key = run_id
        ephemeral_system_prompt = instructions
        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[Optional[Dict]]" = asyncio.Queue()
        created_at = time.time()
        self._run_streams[run_id] = q
        self._run_streams_created[run_id] = created_at
        self._run_approval_sessions[run_id] = approval_session_key

        event_cb = self._make_run_event_callback(run_id, loop)

        def _put_event_if_active(event: Optional[Dict]) -> None:
            """Enqueue only while this run still owns live transport state."""
            if self._run_streams.get(run_id) is q:
                q.put_nowait(event)

        # Also wire stream_delta_callback so message.delta events flow through.
        def _text_cb(delta: Optional[str]) -> None:
            """Queue a ``message.delta`` event, or drop it if the run is gone.

            Called from the agent's worker thread, so the event is posted onto
            the loop with ``call_soon_threadsafe`` rather than enqueued
            directly.
            """
            if delta is None:
                return
            if run_id not in self._run_streams:
                return
            try:
                loop.call_soon_threadsafe(_put_event_if_active, {
                    "event": "message.delta",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "delta": delta,
                })
            except Exception:
                pass

        self._set_run_status(
            run_id,
            "queued",
            created_at=created_at,
            session_id=session_id,
            model=body.get("model", self._model_name),
        )

        # Per-client model routing for /v1/runs (see model_routes).
        route = self._resolve_route(body.get("model"))
        # Background task outlives the HTTP response (and thus the middleware
        # profile scope). Capture now and re-enter inside the task/executor.
        request_profile = _api_request_profile.get()

        async def _run_and_close():
            """Drive the run lifecycle: status, agent, and cleanup.

            Re-checks the stop set right before creating the agent so a stop
            request that raced the status transition is honoured instead of
            starting work that will be abandoned.
            """
            try:
                self._set_run_status(run_id, "running")
                if run_id in self._stopping_run_ids:
                    _put_event_if_active({
                        "event": "run.cancelled",
                        "run_id": run_id,
                        "timestamp": time.time(),
                    })
                    self._set_run_status(
                        run_id,
                        "cancelled",
                        last_event="run.cancelled",
                    )
                    return
                with self._profile_scope(request_profile):
                    agent = self._create_agent(
                        ephemeral_system_prompt=ephemeral_system_prompt,
                        session_id=session_id,
                        stream_delta_callback=_text_cb,
                        tool_progress_callback=event_cb,
                        gateway_session_key=gateway_session_key,
                        route=route,
                    )
                self._active_run_agents[run_id] = agent

                def _approval_notify(approval_data: Dict[str, Any]) -> None:
                    """Redact and broadcast an approval request to the stream."""
                    event = dict(approval_data or {})
                    # Redact credentials from the command before it enters the
                    # SSE/API event stream -- same egress bug as #48456, second
                    # transport: API/desktop clients would otherwise receive the
                    # raw command Tirith flagged. Reuse the gateway seam.
                    if "command" in event:
                        from encre.gateway.run import _redact_approval_command

                        event["command"] = _redact_approval_command(event.get("command"))
                    event.update({
                        "event": "approval.request",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "choices": _approval_event_choices(
                            smart_denied=bool(event.get("smart_denied")),
                            allow_permanent=event.get("allow_permanent") is not False,
                        ),
                    })
                    self._set_run_status(
                        run_id,
                        "waiting_for_approval",
                        last_event="approval.request",
                    )
                    try:
                        loop.call_soon_threadsafe(q.put_nowait, event)
                    except Exception:
                        pass

                def _run_sync():
                    """Run the agent turn with per-run approval/session binding.

                    Runs in a worker thread, so the approval session key and
                    API session context vars are bound here via contextvars
                    and cleared in ``finally``: concurrent runs must not share
                    process-level state.
                    """
                    from encre.gateway.session_context import clear_session_vars
                    from tools.approval import (
                        register_gateway_notify,
                        reset_current_session_key,
                        set_current_session_key,
                        unregister_gateway_notify,
                    )

                    effective_task_id = session_id or run_id
                    approval_token = None
                    session_tokens = []
                    with self._profile_scope(request_profile):
                        try:
                            # Bind approval/session identity for this API run via
                            # contextvars so concurrent runs do not share process
                            # environment state.
                            approval_token = set_current_session_key(approval_session_key)
                            session_tokens = self._bind_api_server_session(
                                session_key=approval_session_key,
                            )
                            register_gateway_notify(approval_session_key, _approval_notify)
                            r = agent.run_conversation(
                                user_message=user_message,
                                conversation_history=conversation_history,
                                task_id=effective_task_id,
                            )
                        finally:
                            try:
                                unregister_gateway_notify(approval_session_key)
                            finally:
                                if approval_token is not None:
                                    try:
                                        reset_current_session_key(approval_token)
                                    except Exception:
                                        pass
                                if session_tokens:
                                    try:
                                        clear_session_vars(session_tokens)
                                    except Exception:
                                        pass
                        u = {
                            "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
                            "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
                            "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
                        }
                        return r, u

                result, usage = await asyncio.get_running_loop().run_in_executor(None, _run_sync)
                if run_id in self._stopping_run_ids:
                    _put_event_if_active({
                        "event": "run.cancelled",
                        "run_id": run_id,
                        "timestamp": time.time(),
                    })
                    self._set_run_status(
                        run_id,
                        "cancelled",
                        last_event="run.cancelled",
                    )
                # Check for structured failure (non-retryable client errors like
                # 401/400 return failed=True instead of raising, so the except
                # block below never fires -- issue #15561).
                elif isinstance(result, dict) and result.get("failed"):
                    error_msg = _redact_api_error_text(result.get("error") or "agent run failed")
                    _put_event_if_active({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": error_msg,
                    })
                    self._set_run_status(
                        run_id,
                        "failed",
                        error=error_msg,
                        last_event="run.failed",
                    )
                else:
                    final_response = result.get("final_response", "") if isinstance(result, dict) else ""
                    _put_event_if_active({
                        "event": "run.completed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "output": final_response,
                        "usage": usage,
                    })
                    self._set_run_status(
                        run_id,
                        "completed",
                        output=final_response,
                        usage=usage,
                        last_event="run.completed",
                    )
            except asyncio.CancelledError:
                self._set_run_status(
                    run_id,
                    "cancelled",
                    last_event="run.cancelled",
                )
                try:
                    _put_event_if_active({
                        "event": "run.cancelled",
                        "run_id": run_id,
                        "timestamp": time.time(),
                    })
                except Exception:
                    pass
                raise
            except Exception as exc:
                logger.exception("[api_server] run %s failed", run_id)
                self._set_run_status(
                    run_id,
                    "failed",
                    error=_redact_api_error_text(exc),
                    last_event="run.failed",
                )
                try:
                    _put_event_if_active({
                        "event": "run.failed",
                        "run_id": run_id,
                        "timestamp": time.time(),
                        "error": _redact_api_error_text(exc),
                    })
                except Exception:
                    pass
            finally:
                # If the asyncio wrapper is cancelled (for example via
                # /stop), the executor thread can still be blocked waiting
                # on an approval Event.  Unregistering here releases those
                # waits immediately; the in-thread unregister is harmlessly
                # idempotent on normal completion.
                try:
                    from tools.approval import unregister_gateway_notify

                    unregister_gateway_notify(approval_session_key)
                except Exception:
                    pass
                # Sentinel: signal SSE stream to close
                try:
                    _put_event_if_active(None)
                except Exception:
                    pass
                self._active_run_agents.pop(run_id, None)
                self._active_run_tasks.pop(run_id, None)
                self._run_approval_sessions.pop(run_id, None)
                self._stopping_run_ids.discard(run_id)

        self._activate_admitted_request()
        task = asyncio.create_task(_run_and_close())
        self._active_run_tasks[run_id] = task
        try:
            self._background_tasks.add(task)
        except TypeError:
            pass
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

        response_headers = (
            {"X-Encre-Session-Key": gateway_session_key} if gateway_session_key else {}
        )
        return web.json_response(
            {"run_id": run_id, "status": "started"},
            status=202,
            headers=response_headers,
        )

    async def _handle_get_run(self, request: "web.Request") -> "web.Response":
        """GET /v1/runs/{run_id} -- return pollable run status for external UIs."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        status = self._run_statuses.get(run_id)
        if status is None:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )
        return web.json_response(status)

    async def _handle_run_events(self, request: "web.Request") -> "web.StreamResponse":
        """GET /v1/runs/{run_id}/events -- SSE stream of structured agent lifecycle events."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]

        # Allow subscribing slightly before the run is registered (race condition window)
        for _ in range(20):
            if run_id in self._run_streams:
                break
            await asyncio.sleep(0.05)
        else:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        q = self._run_streams[run_id]
        self._run_stream_subscribers.add(run_id)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            # Required so a reverse proxy in front of this server flushes each
            # event as it is written; otherwise deltas are buffered until the
            # response closes and the stream looks dead from the client.
            "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
                    continue
                if event is None:
                    # Run finished -- send final SSE comment and close
                    await response.write(b": stream closed\n\n")
                    break
                payload = f"data: {json.dumps(event)}\n\n"
                await response.write(payload.encode())
        except Exception as exc:
            logger.debug("[api_server] SSE stream error for run %s: %s", run_id, exc)
        finally:
            self._run_stream_subscribers.discard(run_id)
            self._run_streams.pop(run_id, None)
            self._run_streams_created.pop(run_id, None)

        return response


    async def _handle_run_approval(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/approval -- resolve a pending run approval."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        status = self._run_statuses.get(run_id)
        if status is None:
            return web.json_response(
                _openai_error(f"Run not found: {run_id}", code="run_not_found"),
                status=404,
            )

        try:
            body = await request.json()
        except Exception:
            return web.json_response(_openai_error("Invalid JSON"), status=400)

        raw_choice = str(body.get("choice", "")).strip().lower()
        # Client-friendly spellings fold onto the canonical "once" so a client
        # posting approve/approved/allow behaves identically to the documented
        # choice set; the comparison stays an exact set lookup either way.
        aliases = {"approve": "once", "approved": "once", "allow": "once"}
        choice = aliases.get(raw_choice, raw_choice)
        allowed = {"once", "session", "always", "deny"}
        if choice not in allowed:
            return web.json_response(
                _openai_error(
                    "Invalid approval choice; expected one of: once, session, always, deny",
                    code="invalid_approval_choice",
                ),
                status=400,
            )

        # Resolution is keyed by the run's own approval session, never the
        # client-supplied session id: two runs sharing a memory session key
        # must not be able to unblock each other's dangerous command.
        approval_session_key = self._run_approval_sessions.get(run_id)
        if not approval_session_key:
            return web.json_response(
                _openai_error(
                    f"Run has no active approval session: {run_id}",
                    code="approval_not_active",
                ),
                status=409,
            )

        # Either accepted key name widens the resolve to every pending
        # approval in the session instead of just the first.
        resolve_all = (
            _coerce_request_bool(body.get("all"), default=False)
            or _coerce_request_bool(body.get("resolve_all"), default=False)
        )
        try:
            from tools.approval import resolve_gateway_approval

            resolved = resolve_gateway_approval(
                approval_session_key,
                choice,
                resolve_all=resolve_all,
            )
        except Exception as exc:
            logger.exception("[api_server] approval resolution failed for run %s", run_id)
            return web.json_response(_openai_error(str(exc)), status=500)

        # A non-positive count means nothing was pending under this key, which
        # is the race between this POST and the agent dropping its own prompt.
        # Reporting it as 409 keeps the client from retrying into a loop.
        if resolved <= 0:
            return web.json_response(
                _openai_error(
                    f"Run has no pending approval: {run_id}",
                    code="approval_not_pending",
                ),
                status=409,
            )

        self._set_run_status(run_id, "running", last_event="approval.responded")
        q = self._run_streams.get(run_id)
        if q is not None:
            try:
                q.put_nowait({
                    "event": "approval.responded",
                    "run_id": run_id,
                    "timestamp": time.time(),
                    "choice": choice,
                    "resolved": resolved,
                })
            except Exception:
                pass

        return web.json_response({
            "object": "encre.run.approval_response",
            "run_id": run_id,
            "choice": choice,
            "resolved": resolved,
        })

    async def _handle_stop_run(self, request: "web.Request") -> "web.Response":
        """POST /v1/runs/{run_id}/stop -- interrupt a running agent."""
        auth_err = self._check_auth(request)
        if auth_err:
            return auth_err

        run_id = request.match_info["run_id"]
        agent = self._active_run_agents.get(run_id)
        task = self._active_run_tasks.get(run_id)

        if agent is None and task is None:
            return web.json_response(_openai_error(f"Run not found: {run_id}", code="run_not_found"), status=404)

        self._set_run_status(run_id, "stopping", last_event="run.stopping")
        self._stopping_run_ids.add(run_id)

        if agent is not None:
            try:
                agent.interrupt("Stop requested via API")
            except Exception:
                pass

        return web.json_response({"run_id": run_id, "status": "stopping"})

    async def _sweep_orphaned_runs(self) -> None:
        """Periodically expire transport buffers and terminal status records."""
        while True:
            await asyncio.sleep(60)
            self._sweep_orphaned_runs_once(time.time())

    def _sweep_orphaned_runs_once(self, now: Optional[float] = None) -> None:
        """Expire old SSE buffers without treating transport age as run age."""
        if now is None:
            now = time.time()
        stale = [
            run_id
            for run_id, created_at in list(self._run_streams_created.items())
            if now - created_at > self._RUN_STREAM_TTL
            and run_id not in self._run_stream_subscribers
        ]
        for run_id in stale:
            logger.debug("[api_server] sweeping expired run transport %s", run_id)
            task = self._active_run_tasks.get(run_id)
            task_done = task is None or task.done()
            if task_done:
                try:
                    from tools.approval import unregister_gateway_notify

                    approval_session_key = self._run_approval_sessions.get(run_id)
                    if approval_session_key:
                        unregister_gateway_notify(approval_session_key)
                except Exception:
                    pass
            # The transport TTL always bounds buffering. Live control state is
            # independent and survives until the executor-backed task returns.
            self._run_streams.pop(run_id, None)
            self._run_streams_created.pop(run_id, None)
            if task_done:
                self._active_run_agents.pop(run_id, None)
                self._active_run_tasks.pop(run_id, None)
                self._run_approval_sessions.pop(run_id, None)
                self._stopping_run_ids.discard(run_id)

        stale_statuses = [
            run_id
            for run_id, status in list(self._run_statuses.items())
            if status.get("status") in {"completed", "failed", "cancelled"}
            and now - float(status.get("updated_at", 0) or 0) > self._RUN_STATUS_TTL
        ]
        for run_id in stale_statuses:
            self._run_statuses.pop(run_id, None)

    # ------------------------------------------------------------------
    # BasePlatformAdapter interface
    # ------------------------------------------------------------------

    def _api_key_passes_startup_guard(self) -> bool:
        """Return True when API_SERVER_KEY is present and strong enough to start."""
        if not self._api_key:
            logger.error(
                "[%s] Refusing to start: API_SERVER_KEY is required for the API server, "
                "including loopback-only binds on %s.",
                self.name, self._host,
            )
            return False

        if not has_usable_secret(self._api_key, min_length=16):
            logger.error(
                "[%s] Refusing to start: API_SERVER_KEY is a "
                "placeholder or too short (<16 chars). This endpoint "
                "dispatches terminal-capable agent work — a guessable "
                "key is remote code execution. Generate a strong secret "
                "(e.g. `openssl rand -hex 32`) and set API_SERVER_KEY "
                "before starting the API server on %s.",
                self.name, self._host,
            )
            return False
        return True

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Start the aiohttp web server."""
        if not AIOHTTP_AVAILABLE:
            logger.warning("[%s] aiohttp not installed", self.name)
            return False

        if not self._api_key_passes_startup_guard():
            return False

        try:
            mws = [
                mw
                for mw in (
                    self._make_profile_prefix_middleware(),
                    cors_middleware,
                    body_limit_middleware,
                    security_headers_middleware,
                )
                if mw is not None
            ]
            self._app = web.Application(middlewares=mws, client_max_size=MAX_REQUEST_BYTES)
            assert self._app is not None
            # Native routes + multiplex /p/<profile>/-- mirrors. Same handlers;
            # the profile-prefix middleware validates the prefix and scopes
            # config/credentials to that profile when multiplexing is on.
            for method, path, handler in self._http_route_table():
                self._app.router.add_route(method, path, handler)
                self._app.router.add_route(method, f"/p/{{profile}}{path}", handler)
            # Store the adapter after native routes are registered. Local Encre-Relay
            # bootstrap shims use this key as a feature-detection hook; registering
            # native routes first lets those shims no-op instead of shadowing the
            # upstream session-control handlers.
            self._app["api_server_adapter"] = self
            if self.gateway_runner is not None:
                self._app["gateway_runner"] = self.gateway_runner

            # Start background sweep to clean up orphaned (unconsumed) run streams
            sweep_task = asyncio.create_task(self._sweep_orphaned_runs())
            try:
                self._background_tasks.add(sweep_task)
            except TypeError:
                pass
            if hasattr(sweep_task, "add_done_callback"):
                sweep_task.add_done_callback(self._background_tasks.discard)

            # Loud warning when a network-accessible API server runs against an
            # unsandboxed local terminal backend. The API server can drive the
            # agent's terminal/file tools as the host user; on a public bind
            # that is the exact surface the 0day campaign abused to write
            # ~/.encre/config.yaml and plant persistence. Sandboxing (Docker /
            # remote backend) contains the blast radius. Warn, don't refuse --
            # the operator may have an external firewall / strong key.
            if is_network_accessible(self._host):
                try:
                    from encre.config import load_config as _load_cfg
                    _backend = (
                        ((_load_cfg() or {}).get("terminal") or {}).get(
                            "backend", "local"
                        )
                    )
                except Exception:
                    _backend = "local"
                if str(_backend).lower() == "local":
                    logger.warning(
                        "[%s] API server is network-accessible (%s) AND the "
                        "terminal backend is 'local' (unsandboxed). Agent work "
                        "dispatched through this endpoint runs as the host user "
                        "with full terminal/file access. Strongly consider a "
                        "sandboxed backend (terminal.backend: docker) and "
                        "firewalling this port to trusted networks only.",
                        self.name, self._host,
                    )

            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            # Bind directly instead of probing 127.0.0.1 first -- the old
            # single-family pre-probe raced the real bind and reported a
            # TIME_WAIT socket as "in use" (#10297), failing gateway
            # restarts for up to ~60s.
            #
            # SO_REUSEADDR is platform-dependent (same rationale as the
            # webhook adapter, #65482):
            #   - macOS (BSD semantics): two sockets with SO_REUSEADDR can
            #     silently split traffic while both report success -- disable.
            #   - Linux: SO_REUSEADDR only permits rebinding past TIME_WAIT
            #     (a second live listener needs SO_REUSEPORT, never set), so
            #     keep the default (enabled) for instant restart rebinds.
            self._site = web.TCPSite(
                self._runner,
                self._host,
                self._port,
                reuse_address=False if sys.platform == "darwin" else None,
            )
            try:
                await self._site.start()
            except OSError as exc:
                await self._runner.cleanup()
                self._runner = None
                self._site = None
                if getattr(exc, "errno", None) == errno.EADDRINUSE:
                    # A port conflict is a configuration error, not a
                    # transient blip -- another process holds the port for
                    # its lifetime. A bare ``return False`` makes the
                    # reconnect watcher in encre.gateway.run treat it as retryable
                    # and loop forever at the backoff cap (observed: 1568+
                    # retries over 5 days across multi-profile setups all
                    # defaulting to the same port, #52132), filling
                    # errors.log and leaking the adapter's ResponseStore
                    # fds each retry. Non-retryable drops it from the
                    # reconnect queue; the operator recovers with
                    # ``/platform resume api_server`` after changing the port.
                    self._set_fatal_error(
                        "api_server_port_in_use",
                        f"Port {self._port} already in use. Set "
                        f"platforms.api_server.port in config.yaml to a "
                        f"different value, then `/platform resume api_server`.",
                        retryable=False,
                    )
                logger.error(
                    "[%s] Could not bind %s:%d: %s. Set a different port in "
                    "config.yaml: platforms.api_server.port",
                    self.name, self._host, self._port, exc,
                )
                return False

            self._mark_connected()
            logger.info(
                "[%s] API server listening on http://%s:%d (model: %s)",
                self.name, self._host, self._port, self._model_name,
            )
            return True

        except Exception as e:
            logger.error("[%s] Failed to start API server: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        """Stop the aiohttp web server and release all owned resources.

        Closes the ResponseStore SQLite connection in addition to stopping
        the aiohttp web server. Without this, every adapter instance leaks
        2 file descriptors (the database file and its WAL sidecar) -- the
        reconnect loop in ``encre.gateway.run`` constructs a fresh adapter on
            every retry, so 2 fds per retry over a 300s cap is about 12 fds/hour, which
        exhausts the default 2560 fd limit after ~12h of failed reconnects
        and turns the whole gateway into a zombie
        (OSError: [Errno 24] Too many open files, #37011).
        """
        self._mark_disconnected()
        if self._response_store is not None:
            try:
                self._response_store.close()
            except Exception:
                logger.debug(
                    "Failed to close response store for %s", self.name, exc_info=True,
                )
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._app = None
        logger.info("[%s] API server stopped", self.name)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Not used -- HTTP request/response cycle handles delivery directly.
        """
        return SendResult(success=False, error="API server uses HTTP request/response, not send()")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about the API server."""
        return {
            "name": "API Server",
            "type": "api",
            "host": self._host,
            "port": self._port,
        }

def _build_adapter(config):
    """Instantiate the API server adapter from platform config.

    Called by the gateway runner when it brings the ``api_server`` platform up;
    the returned adapter is then driven through
    :meth:`encre.gateway.platforms.base.BasePlatformAdapter.connect`.

    Args:
        config: The platform's parsed config section.

    Returns:
        A configured ``APIServerAdapter`` instance.
    """
    return APIAdapter(config)


# -- Platform registration ------------------------------------------------------
from encre.gateway.platform_registry import platform_registry, PlatformEntry
from encre.gateway.config import Platform

def _check_requirements():
    """Report whether the API server platform is available to start.

    Used as the ``check_fn`` of the ``platform_registry`` entry below. Always
    returns ``True``: aiohttp is imported at module load and the platform
    carries no optional third-party dependency to probe.
    """
    return True

platform_registry.register(PlatformEntry(
    name="api_server",
    label="API Server",
    platform=Platform.API_SERVER,
    adapter_factory=_build_adapter,
    check_fn=_check_requirements,
))
