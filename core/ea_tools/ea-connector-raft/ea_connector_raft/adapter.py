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
Raft chat protocol adapter for Encre's IM gateway layer.

Every IM vendor in Encre owns one module under
``core/encre/gateway/platforms/<vendor>/adapter.py`` that implements
:class:`encre.gateway.platforms.base.BasePlatformAdapter`. This module is the
``raft`` entry. :class:`encre.gateway.run.GatewayRunner` drives the adapter
lifecycle; :class:`encre.gateway.stream_consumer.GatewayStreamConsumer`
consumes the
:class:`~encre.gateway.platforms.base.SendResult` values returned by
:meth:`RaftAdapter.send`.

Unlike the REST-style adapters this one runs a *local server* rather than a
client. :meth:`RaftAdapter.connect` binds an aiohttp application on
``platforms.raft.extra.host:port`` (``127.0.0.1:0`` by default, so the OS
picks a free port) exposing four routes:

* ``GET /health``             liveness probe plus activity-queue metrics
* ``POST <path>`` (default ``/wake``)  wake hint from the bridge
* ``POST /activity``         activity event from the bridge
* ``GET /activity/drain``    pull queued activity events

The adapter then spawns ``raft agent bridge`` as a child process, handing it
the bound endpoint and a per-run bearer token. The bridge owns Raft message
cursors and body materialization; it only asks the adapter to wake the
agent, and Encre talks back to Raft through the ``raft`` CLI.

Wake hints are content-free by contract: :func:`_has_content_field` walks the
payload and rejects anything carrying ``body``, ``content``, ``message``,
``text`` and similar keys, so the bridge cannot smuggle chat content into the
agent through the wake path.

A second half of the module is an *outbound* telemetry channel. The gateway
hooks (:func:`_on_session_start` ... :func:`_on_session_finalize`) observe
Encre agent lifecycle events, filter them down to Raft sessions with
:func:`_is_raft_context`, and push :data:`ACTIVITY_EVENT_SCHEMA` events into
every live :class:`RaftAdapter` through :func:`_report_activity`.
:class:`ActivityQueue` bounds that queue so a slow bridge cannot grow memory.

Configuration lives in ``config.yaml`` under ``platforms.raft.extra``
(``host``, ``port``, ``path``, ``bridge_token``, ``runtime_session``,
``max_body_bytes``) with environment fallback via :func:`_env_enablement`,
which auto-enables the platform whenever ``RAFT_PROFILE`` is set.

Public API
----------
``RaftAdapter``                 the adapter registered for platform ``raft``
``ActivityQueue``               bounded, thread-safe activity event queue
``check_raft_requirements``     runtime dependency probe (aiohttp + ``raft`` CLI)
``_is_connected``               connected-platforms probe
``_env_enablement``             env-to-``PlatformConfig.extra`` seeding hook
``interactive_setup``           ``encre gateway setup`` wizard
``_build_adapter``              registry factory
"""


import asyncio

from collections import deque

from datetime import datetime, timezone

import hmac

import json

import logging

import os

import re

import secrets

import shutil

import subprocess

import threading

import time

import uuid

import weakref

from typing import Any, Deque, Dict, List, Optional


from aiohttp import web
AIOHTTP_AVAILABLE = True


import sys

from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))


from encre.gateway.config import Platform, PlatformConfig

from encre.gateway.platforms.base import (

    BasePlatformAdapter,

    MessageEvent,

    MessageType,

    SendResult,

    merge_pending_message_event,

)

from encre.gateway.session import build_session_key


logger = logging.getLogger(__name__)


DEFAULT_HOST = "127.0.0.1"

DEFAULT_PORT = 0

DEFAULT_PATH = "/wake"

DEFAULT_RUNTIME_SESSION = "default"

DEFAULT_MAX_BODY_BYTES = 16_384

DEFAULT_ACTIVITY_QUEUE_CAP = 500

ACTIVITY_CONTENT_CAP = 4096

ACTIVITY_EVENT_SCHEMA = "raft-activity.v1"

ACTIVITY_DRAIN_SCHEMA = "raft-activity-drain.v1"

BRIDGE_TOKEN_HEADER = "x-raft-bridge-token"


_CONTENT_FIELD_NAMES = {

    "body",

    "content",

    "message",

    "messages",

    "preview",

    "snippet",

    "text",

}


_SAFE_SCALAR_RE = re.compile(r"^[a-zA-Z0-9._:@/ -]+$")

_MAX_SCALAR_LENGTH = 120

_ACTIVITY_ALLOWED_FIELDS = {

    "schema",

    "eventId",

    "sessionId",

    "hookEventName",

    "status",

    "occurredAt",

    "toolName",

    "toolInput",

    "toolOutput",

    "toolInputTruncated",

    "toolOutputTruncated",

    "truncated",

    "errorClass",

    "durationMs",

}

_ACTIVE_ADAPTERS: "weakref.WeakSet[RaftAdapter]" = weakref.WeakSet()

_ACTIVE_ADAPTERS_LOCK = threading.Lock()

_RAFT_CONTEXT_LOCK = threading.Lock()

_RAFT_SESSION_IDS: set[str] = set()

_RAFT_TURN_IDS: set[str] = set()

_RAFT_PROMPT_TURN_IDS: set[str] = set()


def check_raft_requirements() -> bool:

    """Probe whether the Raft platform can be instantiated at all.

    Registered as the platform's ``check_fn``, so it runs on every
    ``load_gateway_config()`` -- message handling, display lookups and agent
    turns all call it. It is deliberately silent on failure: logging here
    would flood the logs for every user who does not have the ``raft`` CLI
    installed. The caller (:func:`encre.gateway.platform_registry`'s
    ``create_adapter``) emits its own warning when requirements are missing
    and an adapter is actually requested, which is the convention other
    platform adapters follow.

    Requires both ``aiohttp`` (importable at module load, recorded in
    :data:`AIOHTTP_AVAILABLE`) and a ``raft`` executable on ``PATH``, which
    is needed to spawn the bridge and for the agent's own CLI delivery.

    Returns:
        ``True`` when both prerequisites are satisfied.
    """

    if not AIOHTTP_AVAILABLE:

        return False

    if not shutil.which("raft"):

        return False

    return True


def _path_value(value: Any) -> str:

    """Normalize a configured wake-path into an absolute URL path.

    A configured value may omit the leading slash; aiohttp routes must be
    absolute, so a missing slash is added rather than raising. An empty or
    ``None`` value falls back to :data:`DEFAULT_PATH`, which keeps the
    ``raft`` platform usable with no configuration at all.

    Args:
        value: Raw ``platforms.raft.extra["path"]`` value of any type.

    Returns:
        A non-empty path beginning with ``/``.
    """

    path = str(value or DEFAULT_PATH).strip() or DEFAULT_PATH

    if not path.startswith("/"):

        path = f"/{path}"

    return path


def _has_content_field(value: Any) -> bool:

    """Recursively detect chat-content fields inside a wake payload.

    Wake hints must be content-free: the bridge owns message bodies and must
    only signal that messages are pending. This helper walks dicts (checking
    every key against :data:`_CONTENT_FIELD_NAMES`, case-insensitively and
    ignoring surrounding whitespace) and lists, returning ``True`` as soon as
    a content key appears at any depth.

    Args:
        value: Decoded JSON payload; may be a dict, list, or scalar.

    Returns:
        ``True`` when a content-bearing key is present anywhere in the tree.
    """

    if isinstance(value, dict):

        for key, nested in value.items():

            if str(key).strip().lower() in _CONTENT_FIELD_NAMES:

                return True

            if _has_content_field(nested):

                return True

    elif isinstance(value, list):

        return any(_has_content_field(item) for item in value)

    return False


def _platform_value(value: Any) -> str:

    """Stringify a value that may be an enum member.

    Gateway hooks receive the platform either as a plain string or as a
    :class:`enum.Enum` member; the ``.value`` attribute makes both collapse
    to the same plain string so callers can compare with ``==``. Missing or
    empty values become ``""``.

    Args:
        value: Platform identifier of any type.

    Returns:
        The underlying string, or ``""``.
    """

    return str(getattr(value, "value", value) or "")


def _safe_scalar(value: Any, default: Optional[str] = None) -> Optional[str]:

    """Validate a value as a short, printable scalar suitable for telemetry.

    Activity events cross a process boundary and end up in logs, so the
    fields that identify a session are constrained: they must be ``str``,
    non-empty, at most :data:`_MAX_SCALAR_LENGTH` characters, and matched
    character-by-character by :data:`_SAFE_SCALAR_RE`. This rejects newlines
    (log injection), control characters, and over-long identifiers, while
    still allowing ``session_123``, ``agent@host``, and ``a/b/c``.

    Args:
        value: Candidate value of any type.
        default: Returned when the value fails validation.

    Returns:
        The validated string, or ``default``.
    """

    if not isinstance(value, str):

        return default

    if not value or len(value) > _MAX_SCALAR_LENGTH:

        return default

    if not _SAFE_SCALAR_RE.match(value):

        return default

    return value


def _now_iso() -> str:

    """Return the current UTC time as an RFC 3339 string with a ``Z`` suffix.

    ``datetime.isoformat()`` emits ``+00:00``, which most JSON consumers --
    and the Raft bridge -- expect as ``Z``, so the suffix is rewritten.

    Returns:
        A UTC timestamp such as ``2025-06-11T12:34:56.789012Z``.
    """

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _content_string(value: Any) -> Optional[tuple[str, bool]]:

    """Serialize tool input/output into a capped, single-line-safe string.

    Tool payloads are arbitrary Python objects, so non-strings are JSON
    encoded with sorted keys for deterministic output and ``ensure_ascii=False``
    so non-ASCII content is not double-escaped. The result is truncated to
    :data:`ACTIVITY_CONTENT_CAP` characters; the boolean half of the return
    tuple reports whether truncation happened so the caller can set
    ``toolInputTruncated`` / ``toolOutputTruncated``.

    Args:
        value: A string, or any JSON-serializable object.

    Returns:
        ``(text, truncated)`` on success, ``None`` when the value is
        ``None``, empty after serialization, or not serializable.
    """

    if value is None:

        return None

    if isinstance(value, str):

        text = value

    else:

        try:

            text = json.dumps(value, ensure_ascii=False, sort_keys=True)

        except Exception:

            return None

    if not text:

        return None

    if len(text) > ACTIVITY_CONTENT_CAP:

        return text[:ACTIVITY_CONTENT_CAP], True

    return text, False


def _duration_ms(value: Any) -> Optional[int]:

    """Coerce a duration into a non-negative integer number of milliseconds.

    ``bool`` is rejected explicitly even though it subclasses ``int``: a
    boolean duration is always a bug in the calling hook.

    Args:
        value: Candidate duration, typically a ``float`` or ``int``.

    Returns:
        The truncated duration, or ``None`` when the value is a bool, not a
        number, or negative.
    """

    if not isinstance(value, (int, float)) or isinstance(value, bool):

        return None

    duration = int(value)

    if duration < 0:

        return None

    return duration


def _make_activity_event(

    *,

    hook_event_name: str,

    session_id: Any,

    status: str = "ok",

    tool_name: Any = None,

    tool_input: Any = None,

    tool_output: Any = None,

    error_class: Any = None,

    duration_ms: Any = None,

) -> Dict[str, Any]:

    """Build a :data:`ACTIVITY_EVENT_SCHEMA` event from hook arguments.

    Every field is passed through the same validators the ingest side uses,
    so an event produced here always survives :func:`_validate_activity_event`:
    the session id falls back to ``"unknown"``, scalars are length- and
    charset-checked, the duration is coerced to a non-negative integer, and
    tool input/output are JSON-serialized and truncated to
    :data:`ACTIVITY_CONTENT_CAP`.

    Optional fields are omitted entirely rather than emitted as ``None``,
    which keeps the JSON compact and lets the receiver distinguish "absent"
    from "empty".

    Args:
        hook_event_name: One of ``SessionStart``, ``UserPromptSubmit``,
            ``PreToolUse``, ``PostToolUse``, ``PostToolUseFailure``, ``Stop``
            or ``SessionEnd``.
        session_id: Session the event belongs to.
        status: ``"ok"`` or ``"error"``; anything else normalizes to ``"ok"``.
        tool_name: Tool identifier for tool-use events.
        tool_input: Arguments passed to the tool.
        tool_output: Result, or error message for failures.
        error_class: Short machine-readable failure category.
        duration_ms: Wall-clock duration of the hook's step.

    Returns:
        A dict conforming to :data:`_ACTIVITY_ALLOWED_FIELDS`, including
        ``truncated`` set to ``True`` when any content field was clipped.
    """

    event: Dict[str, Any] = {

        "schema": ACTIVITY_EVENT_SCHEMA,

        "eventId": f"encre-{uuid.uuid4()}",

        "sessionId": _safe_scalar(session_id, "unknown") or "unknown",

        "hookEventName": hook_event_name,

        "status": "error" if status == "error" else "ok",

        "occurredAt": _now_iso(),

    }

    safe_tool_name = _safe_scalar(tool_name)

    if safe_tool_name:

        event["toolName"] = safe_tool_name

    safe_error_class = _safe_scalar(error_class)

    if safe_error_class:

        event["errorClass"] = safe_error_class

    safe_duration_ms = _duration_ms(duration_ms)

    if safe_duration_ms is not None:

        event["durationMs"] = safe_duration_ms

    truncated = False

    input_value = _content_string(tool_input)

    if input_value:

        event["toolInput"], input_truncated = input_value

        if input_truncated:

            event["toolInputTruncated"] = True

            truncated = True

    output_value = _content_string(tool_output)

    if output_value:

        event["toolOutput"], output_truncated = output_value

        if output_truncated:

            event["toolOutputTruncated"] = True

            truncated = True

    if truncated:

        event["truncated"] = True

    return event


def _validate_activity_event(value: Any) -> Dict[str, Any]:

    """Validate and normalize an activity event arriving from the bridge.

    This is the ingest-side twin of :func:`_make_activity_event`: it is what
    makes :class:`ActivityQueue` trustworthy. It rejects non-dicts, unknown
    schemas, and any field outside :data:`_ACTIVITY_ALLOWED_FIELDS` (a closed
    set, so the queue cannot be used to smuggle arbitrary keys past Encre).
    Required identifiers must survive :func:`_safe_scalar`, ``status`` must
    be ``ok`` or ``error``, booleans must actually be booleans, and durations
    must be non-negative numbers.

    Content fields are then re-capped to :data:`ACTIVITY_CONTENT_CAP`, which
    guarantees the queue can never hold an oversized body regardless of how
    the producer constructed the event.

    Args:
        value: Decoded JSON event body.

    Returns:
        A normalized copy of the event.

    Raises:
        ValueError: When any invariant is violated; the message names the
            offending field so the bridge operator can fix the producer.
    """

    if not isinstance(value, dict):

        raise ValueError("activity event must be an object")

    if value.get("schema") != ACTIVITY_EVENT_SCHEMA:

        raise ValueError("unsupported activity event schema")

    unknown = set(value) - _ACTIVITY_ALLOWED_FIELDS

    if unknown:

        raise ValueError(f"activity event field {sorted(unknown)[0]} is not allowed")

    for key in ("eventId", "sessionId", "hookEventName", "occurredAt"):

        if not _safe_scalar(value.get(key)):

            raise ValueError(f"activity event {key} must be a safe non-empty string")

    if value.get("status") not in {"ok", "error"}:

        raise ValueError("activity event status must be ok|error")

    if value.get("toolName") is not None and not _safe_scalar(value.get("toolName")):

        raise ValueError("activity event toolName must be a safe string")

    if value.get("errorClass") is not None and not _safe_scalar(value.get("errorClass")):

        raise ValueError("activity event errorClass must be a safe string")

    if value.get("durationMs") is not None and _duration_ms(value.get("durationMs")) is None:

        raise ValueError("activity event durationMs must be a non-negative number")

    for key in ("truncated", "toolInputTruncated", "toolOutputTruncated"):

        if value.get(key) is not None and not isinstance(value.get(key), bool):

            raise ValueError(f"activity event {key} must be a boolean")

    event = dict(value)

    if event.get("durationMs") is not None:

        event["durationMs"] = _duration_ms(event["durationMs"])

    for key in ("toolInput", "toolOutput"):

        content = event.get(key)

        if content is None:

            continue

        if not isinstance(content, str):

            raise ValueError(f"activity event {key} must be a string")

        if len(content) > ACTIVITY_CONTENT_CAP:

            event[key] = content[:ACTIVITY_CONTENT_CAP]

            event["truncated"] = True

            event[f"{key}Truncated"] = True

    return event


class ActivityQueue:

    """Bounded at-most-once queue for Raft external activity telemetry.

    Encre produces activity events as agent lifecycle hooks fire and the
    bridge pulls them with ``GET /activity/drain``. The two sides run at
    different rates, so the queue must be bounded: when it is full the
    oldest events are dropped and the drop count is reported back to the
    consumer via the ``dropped`` field of a drain response.

    All mutation and reads happen under a single :class:`threading.Lock`,
    which is what makes the queue safe to touch from aiohttp request
    handlers (any thread in the worker pool) and from agent lifecycle hooks
    on the gateway thread.

    Attributes:
        _cap: Maximum number of events held; at least 1.
        _events: FIFO of validated events.
        _dropped_since_drain: Events evicted since the last drain.

    Invariant:
        ``len(_events) <= _cap`` always holds. Overflow is handled by
        popping from the front, never by blocking the producer.
    """

    def __init__(self, cap: int = DEFAULT_ACTIVITY_QUEUE_CAP):

        """Create an empty queue with a lower-bounded capacity.

        Args:
            cap: Maximum number of events to hold. Values that are falsy or
                below 1 are replaced by the default, so a misconfigured
                ``0`` cannot silently disable the queue.
        """

        self._cap = max(1, int(cap or DEFAULT_ACTIVITY_QUEUE_CAP))

        self._events: Deque[Dict[str, Any]] = deque()

        self._dropped_since_drain = 0

        self._lock = threading.Lock()

    def push(self, event: Dict[str, Any]) -> None:

        """Validate and enqueue one event, evicting the oldest on overflow.

        Validation runs *outside* the lock so a malformed event cannot hold
        the queue for other producers; only the append and eviction are
        serialized.

        Args:
            event: Event dict, validated by
                :func:`_validate_activity_event` before enqueue.

        Raises:
            ValueError: If validation fails; the caller decides whether to
                log and drop.
        """

        validated = _validate_activity_event(event)

        with self._lock:

            self._events.append(validated)

            while len(self._events) > self._cap:

                self._events.popleft()

                self._dropped_since_drain += 1

    def drain(self, max_events: int = 200) -> Dict[str, Any]:

        """Remove and return up to ``max_events`` events.

        The drop counter is read and reset inside the same critical section
        as the pop, so a concurrent push cannot hide an eviction from the
        consumer.

        Args:
            max_events: Upper bound on returned events; coerced to at least
                1 so a bad query string cannot return an empty page forever.

        Returns:
            ``{"schema": ACTIVITY_DRAIN_SCHEMA, "events": [...], "dropped":
            n}`` where ``n`` is the number of events evicted since the
            previous drain.
        """

        limit = max(1, int(max_events or 200))

        with self._lock:

            events: List[Dict[str, Any]] = []

            while self._events and len(events) < limit:

                events.append(self._events.popleft())

            dropped = self._dropped_since_drain

            self._dropped_since_drain = 0

        return {"schema": ACTIVITY_DRAIN_SCHEMA, "events": events, "dropped": dropped}

    @property

    def size(self) -> int:

        """Current number of queued events, read under the lock.

        Used by :meth:`RaftAdapter._handle_health` so the health endpoint
        reports a consistent value rather than racing a concurrent push.

        Returns:
            The queue length.
        """

        with self._lock:

            return len(self._events)


def _remember_raft_context(session_id: Any, turn_id: Any = None) -> None:

    """Record that a session (and optionally a turn) belongs to Raft.

    Called from :func:`_is_raft_context` when a hook reports
    ``platform == "raft"``, which is the only authoritative signal that a
    session is Raft-backed. Both identifiers are filtered through
    :func:`_safe_scalar` before being stored, so an untrusted hook argument
    cannot pollute the sets with over-long or control-character-laden keys.

    Args:
        session_id: Session the hook belongs to.
        turn_id: Turn identifier, if the hook supplies one.

    Returns:
        ``None``.
    """

    safe_session_id = _safe_scalar(session_id)

    safe_turn_id = _safe_scalar(turn_id)

    with _RAFT_CONTEXT_LOCK:

        if safe_session_id:

            _RAFT_SESSION_IDS.add(safe_session_id)

        if safe_turn_id:

            _RAFT_TURN_IDS.add(safe_turn_id)


def _forget_raft_context(session_id: Any, turn_id: Any = None, *, forget_session: bool = False) -> None:

    """Drop turn context, and optionally session context, for a finished turn.

    Turn ids are always discarded because turns are short lived and never
    resume. Session ids are discarded only when ``forget_session`` is set,
    because a session can receive several turns and must stay registered
    between them.

    Args:
        session_id: Session to optionally forget.
        turn_id: Turn to forget, plus its ``UserPromptSubmit`` marker.
        forget_session: When ``True``, also drop the session id, which is
            used on session finalization.

    Returns:
        ``None``.
    """

    safe_session_id = _safe_scalar(session_id)

    safe_turn_id = _safe_scalar(turn_id)

    with _RAFT_CONTEXT_LOCK:

        if safe_turn_id:

            _RAFT_TURN_IDS.discard(safe_turn_id)

            _RAFT_PROMPT_TURN_IDS.discard(safe_turn_id)

        if forget_session and safe_session_id:

            _RAFT_SESSION_IDS.discard(safe_session_id)


def _is_raft_context(**kwargs: Any) -> bool:

    """Decide whether a hook invocation belongs to a Raft session.

    Every lifecycle hook starts with this check so activity reporting is
    scoped to Raft and does not leak into Telegram, Discord, or local runs.
    It accepts a platform tag or a previously remembered session/turn id,
    which lets hooks that only receive ``session_id`` still resolve.

    A positive match on the ``platform`` argument also *records* the session
    and turn, so the first Raft hook of a session is what registers it.

    Args:
        **kwargs: The hook's keyword arguments; only ``platform``,
            ``session_id`` and ``turn_id`` are consulted.

    Returns:
        ``True`` when the invocation is Raft-scoped and activity should be
        reported.
    """

    if _platform_value(kwargs.get("platform")) == "raft":

        _remember_raft_context(kwargs.get("session_id"), kwargs.get("turn_id"))

        return True

    safe_session_id = _safe_scalar(kwargs.get("session_id"))

    safe_turn_id = _safe_scalar(kwargs.get("turn_id"))

    with _RAFT_CONTEXT_LOCK:

        return bool(

            (safe_turn_id and safe_turn_id in _RAFT_TURN_IDS)

            or (safe_session_id and safe_session_id in _RAFT_SESSION_IDS)

        )


def _report_activity(event: Dict[str, Any]) -> None:

    """Fan one activity event out to every live :class:`RaftAdapter`.

    The snapshot of :data:`_ACTIVE_ADAPTERS` is taken under
    :data:`_ACTIVE_ADAPTERS_LOCK` and then dispatched outside the lock, so a
    concurrent :meth:`RaftAdapter.disconnect` cannot hold the lock while a
    queue push runs. Iterating a copy also avoids mutation-during-iteration
    errors.

    Args:
        event: A validated
            :data:`ACTIVITY_EVENT_SCHEMA` dict.

    Returns:
        ``None``. A dead adapter cannot raise here because
        :meth:`RaftAdapter.report_activity` swallows validation errors.
    """

    with _ACTIVE_ADAPTERS_LOCK:

        adapters = list(_ACTIVE_ADAPTERS)

    for adapter in adapters:

        adapter.report_activity(event)


def _on_session_start(**kwargs: Any) -> None:

    """Report ``SessionStart`` and register Raft environment passthrough.

    The first thing a Raft session needs is ``RAFT_PROFILE`` to reach the
    correct Raft server, so this hook asks the env-passthrough registry for
    it. That import is best effort: when the tooling module is unavailable
    (for example in a trimmed test environment) the hook still reports the
    session so activity is not lost.

    Args:
        **kwargs: Hook arguments; ``session_id`` is used for the event.

    Returns:
        ``None``.
    """

    if not _is_raft_context(**kwargs):

        return

    try:

        from tools.env_passthrough import register_env_passthrough

        register_env_passthrough(["RAFT_PROFILE"])

    except Exception:

        logger.debug("[raft] failed to register RAFT_PROFILE env passthrough", exc_info=True)

    _report_activity(

        _make_activity_event(

            hook_event_name="SessionStart",

            session_id=kwargs.get("session_id"),

        )

    )


def _on_pre_llm_call(**kwargs: Any) -> None:

    """Report ``UserPromptSubmit`` once per turn.

    The gateway can call this hook more than once for the same turn (for
    example on retry), so :data:`_RAFT_PROMPT_TURN_IDS` records turn ids that
    have already been reported and the duplicate is dropped. The marker is
    cleared by :func:`_forget_raft_context` when the turn ends.

    Args:
        **kwargs: Hook arguments; ``session_id`` and ``turn_id`` are used.

    Returns:
        ``None``.
    """

    if not _is_raft_context(**kwargs):

        return

    safe_turn_id = _safe_scalar(kwargs.get("turn_id"))

    if safe_turn_id:

        with _RAFT_CONTEXT_LOCK:

            if safe_turn_id in _RAFT_PROMPT_TURN_IDS:

                return

            _RAFT_PROMPT_TURN_IDS.add(safe_turn_id)

    _report_activity(

        _make_activity_event(

            hook_event_name="UserPromptSubmit",

            session_id=kwargs.get("session_id"),

        )

    )


def _on_pre_tool_call(**kwargs: Any) -> None:

    """Report ``PreToolUse`` with the tool name and its arguments.

    Args:
        **kwargs: Hook arguments; ``session_id``, ``tool_name`` and ``args``
            are reported. ``args`` is serialized and capped by
            :func:`_content_string`.

    Returns:
        ``None``.
    """

    if not _is_raft_context(**kwargs):

        return

    _report_activity(

        _make_activity_event(

            hook_event_name="PreToolUse",

            session_id=kwargs.get("session_id"),

            tool_name=kwargs.get("tool_name"),

            tool_input=kwargs.get("args"),

        )

    )


def _on_post_tool_call(**kwargs: Any) -> None:

    """Report ``PostToolUse`` or ``PostToolUseFailure`` for a finished tool call.

    Failure is inferred from any of: a ``status`` of ``error`` or
    ``blocked``, or a non-empty ``error_type``. On failure the hook emits the
    failure variant, an ``errorClass`` of the reported type or
    ``tool_failure``, and the error message as the tool output so the bridge
    can show why the step stopped.

    Args:
        **kwargs: Hook arguments; ``session_id``, ``tool_name``, ``args``,
            ``result``, ``error_message``, ``error_type``, ``status`` and
            ``duration_ms`` are reported.

    Returns:
        ``None``.
    """

    if not _is_raft_context(**kwargs):

        return

    status = "error" if kwargs.get("status") in {"error", "blocked"} or kwargs.get("error_type") else "ok"

    hook_name = "PostToolUseFailure" if status == "error" else "PostToolUse"

    _report_activity(

        _make_activity_event(

            hook_event_name=hook_name,

            session_id=kwargs.get("session_id"),

            status=status,

            tool_name=kwargs.get("tool_name"),

            tool_input=kwargs.get("args"),

            tool_output=kwargs.get("error_message") or kwargs.get("result"),

            error_class=kwargs.get("error_type") or ("tool_failure" if status == "error" else None),

            duration_ms=kwargs.get("duration_ms"),

        )

    )


def _on_post_llm_call(**kwargs: Any) -> None:

    """Report ``Stop`` when the model finishes generating a reply.

    Args:
        **kwargs: Hook arguments; ``session_id`` is reported.

    Returns:
        ``None``.
    """

    if not _is_raft_context(**kwargs):

        return

    _report_activity(

        _make_activity_event(

            hook_event_name="Stop",

            session_id=kwargs.get("session_id"),

        )

    )


def _on_session_end(**kwargs: Any) -> None:

    """Report a failed or interrupted session end, then forget the turn.

    Only abnormal endings produce an event: a clean completion already
    reported ``Stop`` from :func:`_on_post_llm_call`, and re-reporting it
    would double-count activity. Interrupted runs and explicitly incomplete
    ones get a ``Stop`` event with ``status`` ``error`` and an
    ``errorClass`` of ``interrupted`` or ``incomplete``.

    Args:
        **kwargs: Hook arguments; ``session_id``, ``turn_id``,
            ``interrupted`` and ``completed`` are consulted.

    Returns:
        ``None``.
    """

    if not _is_raft_context(**kwargs):

        return

    if kwargs.get("interrupted") or kwargs.get("completed") is False:

        _report_activity(

            _make_activity_event(

                hook_event_name="Stop",

                session_id=kwargs.get("session_id"),

                status="error",

                error_class="interrupted" if kwargs.get("interrupted") else "incomplete",

            )

        )

    _forget_raft_context(kwargs.get("session_id"), kwargs.get("turn_id"))


def _on_session_finalize(**kwargs: Any) -> None:

    """Report ``SessionEnd`` and forget the session entirely.

    This is the last hook of a session, so it is the one place
    :func:`_forget_raft_context` is called with ``forget_session=True``;
    leaving the session registered would keep reporting activity for a
    session that no longer exists.

    Args:
        **kwargs: Hook arguments; ``session_id`` and ``turn_id`` are used.

    Returns:
        ``None``.
    """

    if not _is_raft_context(**kwargs):

        return

    _report_activity(

        _make_activity_event(

            hook_event_name="SessionEnd",

            session_id=kwargs.get("session_id"),

        )

    )

    _forget_raft_context(kwargs.get("session_id"), kwargs.get("turn_id"), forget_session=True)


class RaftAdapter(BasePlatformAdapter):

    """Local HTTP endpoint for Raft channel bridge delivery.

    The adapter is a server, not a client: it hosts
    :meth:`_handle_wake`, :meth:`_handle_activity` and
    :meth:`_handle_activity_drain` for the ``raft agent bridge`` process and
    injects wake hints into Encre's normal gateway session pipeline through
    :meth:`handle_message`.

    Outbound delivery is deliberately a no-op in :meth:`send`. Raft messages
    are written by the agent itself through the ``raft`` CLI, which holds
    the correct auth context and cursor state; the adapter returning
    ``SendResult(success=True)`` is what allows
    :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` to proceed
    with its normal flow without duplicating delivery.

    Lifecycle: constructed by :func:`_build_adapter`, brought up by
    :meth:`connect` (which binds the port and spawns the bridge), and torn
    down by :meth:`disconnect` (which kills the bridge and frees the port).
    Each live instance is tracked in :data:`_ACTIVE_ADAPTERS` so the gateway
    hooks can fan activity out without holding a reference that would prevent
    garbage collection.

    Attributes:
        _host: Bind address for the local HTTP server.
        _port: Bind port; ``0`` means the OS chooses a free one.
        _path: URL path that receives wake posts.
        _bridge_token: Shared secret the bridge must present in the
            :data:`BRIDGE_TOKEN_HEADER` header.
        _runtime_session: Chat id used for wake-injected messages.
        _max_body_bytes: Request body cap enforced at the aiohttp layer.
        _runner: aiohttp :class:`web.AppRunner`, ``None`` when disconnected.
        _bridge_process: Spawned bridge :class:`subprocess.Popen`, if any.
        _activity_queue: Bounded queue of inbound activity events.

    Thread/async safety:
        aiohttp request handlers may run on any worker thread, and the
        activity queue is shared with the gateway thread, so all queue
        access is guarded by :class:`ActivityQueue`'s internal lock.
    """

    def __init__(self, config: PlatformConfig):

        """Bind the adapter to its config without opening any socket.

        Args:
            config: Resolved :class:`encre.gateway.config.PlatformConfig`
                for platform ``raft``; ``extra`` carries the tunables.

        Note:
            No bridge token is generated here; :meth:`connect` does that so
            constructing an adapter for a config probe never leaks a usable
            secret.
        """

        super().__init__(config, Platform("raft"))

        extra = config.extra or {}

        self._host: str = str(extra.get("host", DEFAULT_HOST))

        self._port: int = int(extra.get("port", DEFAULT_PORT))

        self._path: str = _path_value(extra.get("path", DEFAULT_PATH))

        self._bridge_token: str = str(extra.get("bridge_token", ""))

        self._runtime_session: str = str(

            extra.get("runtime_session", DEFAULT_RUNTIME_SESSION)

            or DEFAULT_RUNTIME_SESSION

        )

        self._max_body_bytes: int = int(

            extra.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)

        )

        self._runner = None

        self._bridge_process: Optional[subprocess.Popen] = None

        self._activity_queue = ActivityQueue()

    @property

    def runtime_session(self) -> str:

        """Chat id that wake-injected messages are attributed to.

        Returns:
            The configured runtime session name.
        """

        return self._runtime_session

    async def connect(self, *, is_reconnect: bool = False) -> bool:

        """Bind the local server and spawn the Raft bridge child process.

        Steps, in order: mint a bridge token if none was configured, build
        the aiohttp application with a hard request-body cap, register the
        four routes, refuse to start when a configured port is already taken,
        start the server, discover the bound port when ``port == 0``, mark
        the adapter connected, register it in :data:`_ACTIVE_ADAPTERS`, and
        spawn the bridge pointed at the bound endpoint.

        Args:
            is_reconnect: Reserved for interface parity; the Raft adapter
                always performs a full (re)bind.

        Returns:
            ``True`` on success, ``False`` when the configured port is
            already in use. A ``False`` return leaves no runner bound, so
            the gateway runner can retry cleanly.

        Raises:
            Nothing: all failures are logged and converted to ``False`` so a
            broken Raft installation cannot abort gateway startup.
        """

        if not self._bridge_token:

            self._bridge_token = secrets.token_hex(32)

            logger.info("[raft] Auto-generated bridge token")

        # client_max_size makes aiohttp enforce the cap on every read path,
        # including Transfer-Encoding: chunked bodies that carry no
        # Content-Length and would otherwise bypass the header checks below
        # (mirrors gateway/platforms/webhook.py's connect()).

        app = web.Application(client_max_size=self._max_body_bytes)

        app.router.add_get("/health", self._handle_health)

        app.router.add_post(self._path, self._handle_wake)

        app.router.add_post("/activity", self._handle_activity)

        app.router.add_get("/activity/drain", self._handle_activity_drain)

        if self._port != 0:

            import socket as _socket

            # Probe before binding: a listener on the same port would make
            # TCPSite.start() raise, which is harder to diagnose than this.

            try:

                with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:

                    sock.settimeout(1)

                    sock.connect(("127.0.0.1", self._port))

                logger.error(

                    "[raft] Port %d already in use. Set platforms.raft.extra.port in config",

                    self._port,

                )

                return False

            except (ConnectionRefusedError, OSError):

                pass

        self._runner = web.AppRunner(app)

        await self._runner.setup()

        site = web.TCPSite(self._runner, self._host, self._port)

        await site.start()

        # Port 0 means "pick a free port"; read back the real value so the
        # bridge is told the address it must actually POST to.

        bound_port = self._port

        if bound_port == 0 and site._server and site._server.sockets:

            bound_port = site._server.sockets[0].getsockname()[1]

        self._mark_connected()

        with _ACTIVE_ADAPTERS_LOCK:

            _ACTIVE_ADAPTERS.add(self)

        logger.info("[raft] Raft channel listening on %s:%d%s", self._host, bound_port, self._path)

        self._spawn_bridge(bound_port)

        return True

    async def disconnect(self) -> None:

        """Stop the bridge, tear down the server, and unregister the adapter.

        The bridge is stopped first so it cannot POST to a server that is
        already closing, and the adapter is removed from
        :data:`_ACTIVE_ADAPTERS` before :meth:`_mark_disconnected` so the
        gateway hooks stop fanning events at a dying instance.

        Returns:
            ``None``. Idempotent: every field is ``None``-checked, so
            disconnecting an adapter that never connected is safe.
        """

        self._stop_bridge()

        if self._runner:

            await self._runner.cleanup()

            self._runner = None

        with _ACTIVE_ADAPTERS_LOCK:

            _ACTIVE_ADAPTERS.discard(self)

        self._mark_disconnected()

        logger.info("[raft] Disconnected")

    def _spawn_bridge(self, port: int) -> None:

        """Spawn ``raft agent bridge`` pointed at the local wake endpoint.

        The bridge is told the exact bound port (which may differ from the
        configured value when the port was ``0``) and receives the shared
        secret through the ``RAFT_CHANNEL_TOKEN`` environment variable, so it
        never appears on the process command line. ``stdin`` is detached so
        the child cannot inherit the gateway's terminal and block on it.

        Args:
            port: The port the HTTP server actually bound to.

        Returns:
            ``None``. A missing ``raft`` binary or missing ``RAFT_PROFILE``
            is a warning, not a failure: the adapter still serves wake posts
            and the operator can run the bridge manually.
        """

        raft_bin = shutil.which("raft")

        if not raft_bin:

            logger.warning("[raft] raft CLI not found in PATH; bridge not spawned 鈥?wake-only polling mode")

            return

        profile = os.environ.get("RAFT_PROFILE", "")

        if not profile:

            logger.warning("[raft] RAFT_PROFILE not set; bridge not spawned")

            return

        endpoint = f"http://{self._host}:{port}{self._path}"

        cmd: List[str] = [

            raft_bin, "--profile", profile,

            "agent", "bridge",

            "--wake-adapter", "wake-channel",

            "--wake-channel-endpoint", endpoint,

        ]

        env = {**os.environ, "RAFT_CHANNEL_TOKEN": self._bridge_token}

        try:

            self._bridge_process = subprocess.Popen(

                cmd, env=env, stdin=subprocess.DEVNULL

            )

            logger.info("[raft] Spawned bridge pid=%d profile=%s endpoint=%s", self._bridge_process.pid, profile, endpoint)

        except Exception:

            logger.exception("[raft] Failed to spawn bridge")

    def _stop_bridge(self) -> None:

        """Terminate the bridge child process, escalating to kill on timeout.

        The reference is cleared before signalling so a concurrent call
        cannot terminate the same process twice. A 5 second graceful window
        is given before :meth:`~subprocess.Popen.kill`.

        Returns:
            ``None``. Safe to call when no bridge was spawned.
        """

        proc = self._bridge_process

        if proc is None:

            return

        self._bridge_process = None

        try:

            proc.terminate()

            proc.wait(timeout=5)

            logger.info("[raft] Bridge process terminated (pid=%d)", proc.pid)

        except subprocess.TimeoutExpired:

            proc.kill()

            logger.warning("[raft] Bridge process killed after timeout (pid=%d)", proc.pid)

        except Exception:

            logger.exception("[raft] Error stopping bridge")

    async def send(

        self,

        chat_id: str,

        content: str,

        reply_to: Optional[str] = None,

        metadata: Optional[Dict[str, Any]] = None,

    ) -> SendResult:

        """No-op outbound hook: the agent delivers Raft messages via the CLI.

        Raft delivery needs the agent's own auth context and message cursor,
        neither of which the adapter holds, so sending here would either
        duplicate the agent's own ``raft`` CLI call or silently drop content.
        Returning ``success=True`` tells
        :class:`encre.gateway.stream_consumer.GatewayStreamConsumer` that the
        turn may continue; the actual message is written by the agent.

        Args:
            chat_id: Target chat; ignored.
            content: Agent output; ignored.
            reply_to: Reply target; ignored.
            metadata: Extra send hints; ignored.

        Returns:
            ``SendResult(success=True)``, always.
        """

        logger.debug("[raft] adapter send is a no-op; agent delivers via raft CLI")

        return SendResult(success=True)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:

        """Describe a Raft chat without contacting any server.

        Raft chat metadata is owned by the bridge and the Raft server, which
        the adapter does not query. A synthetic name is enough for the
        display paths that call this method.

        Args:
            chat_id: Chat id to describe.

        Returns:
            ``{"name": "raft/<chat_id>", "type": "raft"}``.
        """

        return {"name": f"raft/{chat_id}", "type": "raft"}

    async def _handle_health(self, request: "web.Request") -> "web.Response":

        """``GET /health`` -- liveness probe with activity-queue metrics.

        Deliberately unauthenticated: operators and orchestrators need a
        cheap way to confirm the adapter is listening and to see how backed
        up the activity queue is. The payload reports the runtime session
        and both activity endpoints so a bridge misconfiguration is visible
        from a single request.

        Args:
            request: Incoming aiohttp request; unused.

        Returns:
            A 200 JSON response with status, platform, runtime session and
            activity metrics.
        """

        return web.json_response(

            {

                "status": "ok",

                "platform": "raft",

                "runtimeSession": self._runtime_session,

                "activity": {

                    "queueSize": self._activity_queue.size,

                    "endpoint": "/activity",

                    "drainEndpoint": "/activity/drain",

                },

            }

        )

    async def _handle_wake(self, request: "web.Request") -> "web.Response":

        """``POST <path>`` -- accept a content-free wake hint from the bridge.

        Processing order, each step short-circuiting with a specific status
        code:

        1. **auth** -- the :data:`BRIDGE_TOKEN_HEADER` header must match the
           shared secret (401 otherwise).
        2. **size** -- ``Content-Length`` above the cap is rejected before
           any bytes are read (413), and the same cap is re-checked on the
           bytes actually read, which catches chunked bodies that carry no
           ``Content-Length``.
        3. **JSON** -- an empty body is allowed (a bare wake signal), but a
           non-empty body must parse as a JSON object (400 otherwise).
        4. **content-free** -- :func:`_has_content_field` rejects payloads
           that smuggle message content (400).
        5. **dispatch** -- :meth:`_accept_wake` injects the wake event (503
           when the gateway message handler is not attached yet, so the
           bridge can retry).

        The payload's ``schema`` field is intentionally not gated: the bridge
        owns schema evolution and Encre only needs to verify that wake hints
        stay content-free.

        Args:
            request: Incoming aiohttp request with the wake payload.

        Returns:
            A JSON response: 202 when accepted, 401/400/413/503 for the
            failure modes above.
        """

        if not self._validate_bridge_token(request.headers.get(BRIDGE_TOKEN_HEADER, "")):

            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        content_length = request.content_length or 0

        if content_length > self._max_body_bytes:

            return web.json_response({"ok": False, "error": "payload_too_large"}, status=413)

        try:

            raw_body = await request.read()

        except web.HTTPRequestEntityTooLarge:

            # aiohttp's client_max_size tripped - chunked or lying
            # Content-Length. Same 413 as the header check above.

            return web.json_response({"ok": False, "error": "payload_too_large"}, status=413)

        except Exception:

            return web.json_response({"ok": False, "error": "bad_request"}, status=400)

        if len(raw_body) > self._max_body_bytes:

            # Defense in depth: enforce the cap on the actual bytes read even
            # if the server-level limit was bypassed or misconfigured.

            return web.json_response({"ok": False, "error": "payload_too_large"}, status=413)

        payload: Dict[str, Any] = {}

        if raw_body.strip():

            try:

                parsed = json.loads(raw_body)

            except json.JSONDecodeError:

                return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

            if not isinstance(parsed, dict):

                return web.json_response({"ok": False, "error": "invalid_payload"}, status=400)

            payload = parsed

        # Do not gate on payload["schema"]: the bridge owns schema evolution;
        # Encre only verifies that wake hints are content-free.

        if _has_content_field(payload):

            return web.json_response({"ok": False, "error": "content_not_allowed"}, status=400)

        accepted = await self._accept_wake(payload)

        if not accepted:

            return web.json_response(

                {

                    "ok": False,

                    "error": "not_ready",

                    "runtimeSession": self._runtime_session,

                },

                status=503,

            )

        return web.json_response(

            {

                "ok": True,

                "runtimeSession": self._runtime_session,

            },

            status=202,

        )

    async def _handle_activity(self, request: "web.Request") -> "web.Response":

        """``POST /activity`` -- accept one activity event from the bridge.

        Mirrors the guard chain of :meth:`_handle_wake` (auth, then body cap
        on both the declared and the actual size) and then pushes the event
        through :class:`ActivityQueue`, which validates it against
        :data:`_ACTIVITY_ALLOWED_FIELDS`. A 202 is returned as soon as the
        event is queued, so the bridge is not coupled to any downstream
        consumer.

        Args:
            request: Incoming aiohttp request with a JSON event body.

        Returns:
            A JSON response: 202 when queued, 401/400/413 for auth,
            validation and size failures. Validation errors return the
            exception text so the bridge can fix its producer.
        """

        if not self._validate_bridge_token(request.headers.get(BRIDGE_TOKEN_HEADER, "")):

            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        content_length = request.content_length or 0

        if content_length > self._max_body_bytes:

            return web.json_response({"ok": False, "error": "payload_too_large"}, status=413)

        try:

            raw_text = await request.text()

        except web.HTTPRequestEntityTooLarge:

            # aiohttp's client_max_size tripped - chunked or lying
            # Content-Length. Same 413 as the header check above.

            return web.json_response({"ok": False, "error": "payload_too_large"}, status=413)

        except Exception as exc:

            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        if len(raw_text.encode("utf-8")) > self._max_body_bytes:

            # Defense in depth: enforce the cap on the actual bytes read even
            # if the server-level limit was bypassed or misconfigured.

            return web.json_response({"ok": False, "error": "payload_too_large"}, status=413)

        try:

            payload = json.loads(raw_text)

            self._activity_queue.push(payload)

        except json.JSONDecodeError:

            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

        except Exception as exc:

            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        return web.json_response({"ok": True}, status=202)

    async def _handle_activity_drain(self, request: "web.Request") -> "web.Response":

        """``GET /activity/drain`` -- pull queued activity events.

        This is the pull side of the activity channel: the bridge calls it on
        a timer and receives everything queued since the previous call, plus
        the number of events that were evicted for lack of space. A bad
        ``?max=`` value falls back to 200 rather than erroring, so a typo in
        the bridge cannot stall its reporting loop.

        Args:
            request: Incoming aiohttp request; ``query["max"]`` bounds the
                number of returned events.

        Returns:
            A 200 JSON response shaped as
            ``{"schema": ACTIVITY_DRAIN_SCHEMA, "events": [...], "dropped": n}``.
        """

        if not self._validate_bridge_token(request.headers.get(BRIDGE_TOKEN_HEADER, "")):

            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:

            max_events = int(request.query.get("max", "200"))

        except ValueError:

            max_events = 200

        return web.json_response(self._activity_queue.drain(max_events))

    def _validate_bridge_token(self, token: str) -> bool:

        """Constant-time check of the bridge's shared secret.

        :func:`hmac.compare_digest` is used because the token guards the only
        unauthenticated entry point into the gateway, and a short-circuiting
        ``==`` would leak position information through timing. Both values
        are encoded to bytes first: ``compare_digest`` raises ``TypeError``
        for strings containing non-ASCII characters, and this value comes
        straight from a raw request header.

        Args:
            token: Value of the :data:`BRIDGE_TOKEN_HEADER` header.

        Returns:
            ``False`` when either side is empty, otherwise whether the tokens
            match.
        """

        if not self._bridge_token or not token:

            return False

        # Compare as bytes: compare_digest raises TypeError on a str with
        # non-ASCII characters, and the token is a raw request header.

        return hmac.compare_digest(token.encode(), self._bridge_token.encode())

    async def _accept_wake(self, payload: Dict[str, Any]) -> bool:

        """Inject a wake hint into the gateway session pipeline.

        The delivery id is resolved from a chain of payload keys because the
        bridge has renamed the field over versions; when none is present a
        time-based id keeps :meth:`handle_message` deduplication working. The
        injected event is marked ``internal=True`` so downstream filters
        know it is synthetic rather than user-typed, and the text is the
        content-free prompt from :meth:`_wake_prompt`.

        Args:
            payload: Content-free wake payload, possibly empty.

        Returns:
            ``True`` when the event was handed to the gateway, ``False``
            when no message handler is attached yet or injection raised. A
            ``False`` return makes :meth:`_handle_wake` answer 503 so the
            bridge retries.
        """

        if not self._message_handler:

            logger.warning("[raft] Wake received before gateway message handler was attached")

            return False

        delivery_id = str(

            payload.get("eventId")

            or payload.get("attemptId")

            or payload.get("messageId")

            or payload.get("delivery_id")

            or payload.get("wake_id")

            or payload.get("id")

            or f"raft-wake-{int(time.time() * 1000)}"

        )

        source = self.build_source(

            chat_id=self._runtime_session,

            chat_name="Raft channel",

            chat_type="dm",

            user_id="raft-bridge",

            user_name="Raft Bridge",

        )

        event = MessageEvent(

            text=self._wake_prompt(),

            message_type=MessageType.TEXT,

            source=source,

            raw_message=payload,

            message_id=delivery_id,

            internal=True,

        )

        try:

            await self.handle_message(event)

        except Exception:

            logger.exception("[raft] Failed to inject wake event")

            return False

        return True

    async def handle_message(self, event: MessageEvent) -> None:

        """Accept Raft wake hints without interrupting an active Encre turn.

        When the session this wake belongs to is already running a turn, the
        event is merged into that session's pending queue instead of starting
        a new turn: two turns for one session would race on the same agent
        state. Otherwise the base implementation is used unchanged.

        Args:
            event: Wake-injected
                :class:`~encre.gateway.platforms.base.MessageEvent`.

        Returns:
            ``None``. Events for a session with no message handler are
            dropped silently; the wake will be retried by the bridge.
        """

        if not self._message_handler:

            return

        session_key = build_session_key(

            event.source,

            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),

            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),

        )

        if session_key in self._active_sessions:

            logger.debug("[raft] Wake queued for busy session %s", session_key)

            merge_pending_message_event(self._pending_messages, session_key, event)

            return

        await super().handle_message(event)

    @staticmethod

    def _wake_prompt() -> str:

        """Build the text injected into the agent for a wake hint.

        The prompt intentionally carries no message content: it only tells
        the agent that messages may be pending and reminds it to consult the
        Raft CLI manual before issuing Raft commands, which is how the agent
        is expected to fetch the real bodies.

        Returns:
            The wake prompt string.
        """

        return (

            "Raft wake hint received. New Raft messages may be pending. "

            "If you have not read the Raft manual in this session, run "

            "`raft manual get raft-cli-overview` before using Raft commands."

        )

    def report_activity(self, event: Dict[str, Any]) -> None:

        """Enqueue one activity event, never raising into the calling hook.

        Called by :func:`_report_activity` from agent lifecycle hooks, which
        must not be able to break a turn because of a malformed telemetry
        event. Validation failures are logged at debug and dropped.

        Args:
            event: Event dict conforming to
                :data:`ACTIVITY_EVENT_SCHEMA`.

        Returns:
            ``None``.
        """

        try:

            self._activity_queue.push(event)

        except Exception:

            logger.debug("[raft] activity event dropped during validation", exc_info=True)


def _is_connected(config: PlatformConfig) -> bool:

    """Report whether the Raft platform is enabled for its config.

    Used by the connected-platforms view, which runs before any adapter is
    constructed. Only the two keys that indicate an intentional Raft setup
    are accepted, so a bare ``extra`` mapping cannot mark the platform as
    connected by accident.

    Args:
        config: :class:`encre.gateway.config.PlatformConfig` for ``raft``.

    Returns:
        ``True`` when ``extra["enabled"]`` or ``extra["bridge_token"]`` is
        set.
    """

    extra = config.extra or {}

    return bool(extra.get("enabled") or extra.get("bridge_token"))


def _env_enablement() -> Optional[dict]:

    """Seed ``PlatformConfig.extra`` from the environment during config load.

    Called by the platform registry's env-enablement hook before adapter
    construction, so ``encre gateway status`` reflects an env-only setup
    without instantiating an adapter. The platform auto-enables when
    ``RAFT_PROFILE`` is set, because that variable is required to spawn the
    bridge anyway; a setup with a profile but no ``config.yaml`` block is
    therefore usable out of the box.

    Returns:
        ``{"enabled": True}`` when ``RAFT_PROFILE`` is set, otherwise
        ``None`` so the caller skips auto-enabling.
    """

    if not os.getenv("RAFT_PROFILE"):

        return None

    return {"enabled": True}


def interactive_setup() -> None:

    """Interactive ``encre gateway setup`` flow for the Raft platform.

    CLI helpers are imported inside the function so the plugin stays
    importable in gateway runtime and test contexts that do not ship the CLI
    layer. The flow persists ``RAFT_PROFILE`` to the Encre env file, which is
    what makes :func:`_env_enablement` auto-enable the adapter after a
    gateway restart.

    An existing profile short-circuits the wizard unless the operator asks to
    reconfigure, so re-running setup never silently overwrites a working
    configuration.

    Args:
        None; all input comes from the terminal.

    Returns:
        ``None``. Aborts early with a warning when no profile slug is
        supplied.
    """

    from encre.setup import (

        print_header,

        print_info,

        print_success,

        print_warning,

        prompt,

        prompt_yes_no,

    )

    from encre.config import get_env_value, save_env_value

    print_header("Raft")

    existing_profile = get_env_value("RAFT_PROFILE")

    if existing_profile:

        print_info(f"Raft: already configured (profile: {existing_profile})")

        if not prompt_yes_no("Reconfigure Raft?", False):

            print_info(f"Keeping RAFT_PROFILE={existing_profile}.")

            return

    print_info("Connect Encre to Raft as an external agent.")

    print_info("Create the External Agent in Raft first, then run:")

    print_info("  raft agent login --server <server-url> --agent <agent-id> --profile-slug <slug>")

    print()

    profile = prompt("Raft profile slug", default=existing_profile or "")

    if not profile:

        print_warning("Raft profile slug is required; skipping Raft setup")

        return

    save_env_value("RAFT_PROFILE", profile.strip())

    print()

    print_success("Raft configuration saved")


def _build_adapter(config):

    """Registry factory for :class:`RaftAdapter`.

    Args:
        config: Resolved :class:`encre.gateway.config.PlatformConfig` for
            platform ``raft``, passed straight through.

    Returns:
        A new :class:`RaftAdapter`. No socket is bound and no bridge is
        spawned at construction time, so config probes stay cheap.
    """

    return RaftAdapter(config)

