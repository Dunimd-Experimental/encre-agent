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

"""The single source of truth for projecting :data:`AgentEvent` onto wire frames.

Every transport that ships agent-loop events to a consumer -- the desktop
WebSocket bridge (``encre.protocol.handlers.events``) and the headless
JSONL transport (``encre.headless``) -- projects events through
:func:`project_event`.  Keeping the projection here, in the harness, means a
new event type can never reach one transport and silently vanish on the
other, and the frame vocabulary (``text_delta``, ``tool_result``, ``finish``,
...) has exactly one definition.

The projection is a *total* function over the known event types: an event
whose frame depends only on the event itself (plus the session id) is fully
projected; the two frames that also carry a session snapshot
(``compact``/``finish``) are projected from the optional
:class:`SessionSnapshot` passed by the caller.  An unknown event type yields
``None`` rather than raising, so a transport never crashes on a
forward-compatible event it does not understand.

Typical use::

    frame = project_event(event, session_id=sid, snapshot=snap)
    if frame is not None:
        await send(frame.name, **frame.payload)
"""

import json
from dataclasses import dataclass, field
from typing import Any

from encre.logging_config import get_logger
from encre.utils.types import (
    Artifact,
    AssistantBoundary,
    BackendError,
    CompactFailed,
    CompactNotification,
    CompactStarting,
    EngineInstallProgress,
    EngineInstallRequest,
    Finish,
    ModelSwitch,
    PermissionRequest,
    PlanUpdate,
    QuestionRequest,
    Reference,
    RetryNotice,
    SystemMessage,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolProgress,
    ToolResult,
    WorkflowCompletedEvent,
    WorkflowStartedEvent,
    WorkflowTaskEvent,
)

logger = get_logger(__name__)

#: Maximum number of characters of a tool result carried on the wire.  A tool
#: may legitimately produce megabytes (a whole file, a test log); the wire
#: frame stays bounded so a single result cannot stall the transport.
TOOL_RESULT_WIRE_LIMIT = 100_000

#: Suffix appended to a tool result truncated at :data:`TOOL_RESULT_WIRE_LIMIT`.
TOOL_RESULT_TRUNCATION_SUFFIX = "\n... (truncated)"

#: ``SystemMessage.content`` prefix carrying a parsed spec document.
SPEC_DATA_PREFIX = "__spec_data__:"

#: ``SystemMessage.content`` prefix carrying a parsed plan review.
PLAN_REVIEW_PREFIX = "__plan_review__:"


@dataclass
class WireFrame:
    """A projected wire message: ``{"type": name, **payload}``.

    Attributes:
        name: The frame type the consumer dispatches on.
        payload: Every field of the frame except ``type``.
    """

    name: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_message(self) -> dict[str, Any]:
        """Return the frame as the flat ``{"type": ..., **payload}`` mapping.

        Returns:
            A new dict; mutating it never affects this frame.
        """
        return {"type": self.name, **self.payload}


@dataclass
class SessionSnapshot:
    """Session-derived extras required by the snapshot-bearing frames.

    Only ``compact`` and ``finish`` need more than the event itself: they
    resend the conversation (so a client that compacted away messages stays
    in sync) and, for ``finish``, the id of the last assistant message.  A
    transport builds this once per dispatched event; passing ``None`` instead
    of an empty snapshot produces exactly the "no session" shaping the
    WebSocket bridge used to hardcode.

    Attributes:
        renderer_messages: Visible history (model-only compaction blocks
            stripped) as the renderer expects it.
        tail_message: The raw last session message, used to resolve the
            ``assistant_message_id`` reported on ``finish``.
        plan_items: The session's current plan items.
        artifacts: The session's current artifacts.
        references: The session's current references.
    """

    renderer_messages: list[dict[str, Any]] = field(default_factory=list)
    tail_message: dict[str, Any] | None = None
    plan_items: Any = None
    artifacts: Any = None
    references: Any = None


@dataclass(frozen=True)
class SnapshotNeed:
    """What a frame requires from the session snapshot.

    Attributes:
        required: Whether the frame carries any session-derived field.
        renderer_history: Whether the O(n) renderer history is actually used.
            ``finish`` only carries it while ``compacted``, so a transport
            must not render the conversation for every ordinary end-of-turn.
    """

    required: bool = False
    renderer_history: bool = False


def session_snapshot_need(event: Any) -> SnapshotNeed:
    """Return what ``event`` needs from the session snapshot.

    A transport calls this *before* building a snapshot so the projection
    stays lazy: the rule of which frames carry session state lives here, not
    twice in the transports.

    Args:
        event: The agent event about to be projected.

    Returns:
        The :class:`SnapshotNeed` for that event.
    """
    if isinstance(event, CompactNotification):
        return SnapshotNeed(required=True, renderer_history=True)
    if isinstance(event, Finish):
        return SnapshotNeed(required=True, renderer_history=bool(event.compacted))
    return SnapshotNeed()


def project_event(
    event: Any,
    *,
    session_id: str | None = None,
    snapshot: SessionSnapshot | None = None,
) -> WireFrame | None:
    """Project one agent event onto its wire frame.

    Args:
        event: An :data:`~encre.utils.types.AgentEvent` instance.
        session_id: Session the frame belongs to; stamped on every payload
            (``None`` is a legitimate value and is preserved as such).
        snapshot: Session extras for ``compact``/``finish``.  When ``None``
            those frames are shaped exactly as if no session were attached.

    Returns:
        The projected frame, or ``None`` when the event carries no frame
        (empty deltas, an unknown type, or a malformed ``SystemMessage``
        payload).
    """
    frame = _project(event, snapshot)
    if frame is None:
        return None
    frame.payload["session_id"] = session_id
    return frame


def _project(event: Any, snapshot: SessionSnapshot | None) -> WireFrame | None:
    """Project ``event`` without the session id stamp.

    Args:
        event: The agent event to project.
        snapshot: Session extras, or ``None``.

    Returns:
        The frame (without ``session_id``), or ``None``.
    """
    if isinstance(event, TextDelta):
        if not event.text:
            return None
        return WireFrame("text_delta", {"text": event.text})

    if isinstance(event, ThinkingDelta):
        if not event.text:
            return None
        return WireFrame("thinking_delta", {"text": event.text})

    if isinstance(event, ToolCallStart):
        return WireFrame("tool_call_start", {"name": event.name, "id": event.id})

    if isinstance(event, ToolCallDelta):
        return WireFrame(
            "tool_call_delta", {"id": event.id, "key": event.key, "value": event.value},
        )

    if isinstance(event, ToolCallEnd):
        return WireFrame("tool_call_end", {"id": event.id})

    if isinstance(event, ToolProgress):
        return WireFrame(
            "tool_progress",
            {
                "id": event.id,
                "tool_name": event.tool_name,
                "status": event.status,
                "sub_agent_messages": event.sub_agent_messages,
                "content": event.content,
            },
        )

    if isinstance(event, ToolResult):
        content = event.content
        if len(content) > TOOL_RESULT_WIRE_LIMIT:
            content = content[:TOOL_RESULT_WIRE_LIMIT] + TOOL_RESULT_TRUNCATION_SUFFIX
        return WireFrame(
            "tool_result",
            {
                "id": event.id,
                "content": content,
                "is_error": event.is_error,
                "sub_agent_messages": event.sub_agent_messages,
                "sub_agent_session_id": event.sub_agent_session_id,
            },
        )

    if isinstance(event, PermissionRequest):
        return WireFrame(
            "permission_request",
            {"tool_name": event.tool_name, "reason": event.reason},
        )

    if isinstance(event, QuestionRequest):
        return WireFrame(
            "question_request",
            {"tool_call_id": event.tool_call_id, "questions": event.questions},
        )

    if isinstance(event, Artifact):
        return WireFrame("artifacts_update", {"artifacts": [event.artifact]})

    if isinstance(event, BackendError):
        return WireFrame(
            "error",
            {
                "message": event.error,
                "code": event.code or "backend_error",
                "category": event.category or "unknown",
                "retryable": event.retryable,
                "retry_after": event.retry_after,
                "details": event.details or {},
            },
        )

    if isinstance(event, Reference):
        return WireFrame("references_update", {"references": [event.reference]})

    if isinstance(event, PlanUpdate):
        return WireFrame("plan_update", {"plan_items": event.plan_items})

    if isinstance(event, AssistantBoundary):
        return WireFrame("assistant_boundary", {})

    if isinstance(event, CompactStarting):
        return WireFrame(
            "compact_starting",
            {"notice_id": event.notice_id, "anchor_id": event.anchor_id},
        )

    if isinstance(event, RetryNotice):
        # One frame for every retry source: the client renders a single
        # "retrying" line and needs no knowledge of which layer decided to
        # retry, only why and for how long.
        return WireFrame(
            "retry",
            {
                "scope": event.scope,
                "attempt": event.attempt,
                "max_attempts": event.max_attempts,
                "delay_ms": event.delay_ms,
                "message": event.message,
                "status_code": event.status_code,
                "error_class": event.error_class,
            },
        )

    if isinstance(event, CompactFailed):
        return WireFrame(
            "compact_failed",
            {"reason": event.reason or "", "notice_id": event.notice_id},
        )

    if isinstance(event, CompactNotification):
        return WireFrame(
            "compact",
            {
                "old_count": event.old_count,
                "new_count": event.new_count,
                "old_tokens": event.old_tokens,
                "new_tokens": event.new_tokens,
                "notice_id": event.notice_id,
                "messages": snapshot.renderer_messages if snapshot else [],
                "plan_items": snapshot.plan_items if snapshot else None,
                "artifacts": snapshot.artifacts if snapshot else None,
                "references": snapshot.references if snapshot else None,
            },
        )

    if isinstance(event, ModelSwitch):
        return WireFrame(
            "model_switch",
            {
                "from_model": event.from_model,
                "to_model": event.to_model,
                "reason": event.reason or "",
                "source": event.source or "manual",
                "notice_id": event.notice_id,
            },
        )

    if isinstance(event, SystemMessage):
        return _project_system_message(event)

    if isinstance(event, WorkflowStartedEvent):
        return WireFrame(
            "workflow_started",
            {
                "workflow_id": event.workflow_id,
                "goal": event.goal,
                "total_tasks": event.total_tasks,
                "task_ids": event.task_ids,
            },
        )

    if isinstance(event, WorkflowTaskEvent):
        return WireFrame(
            "workflow_task",
            {
                "workflow_id": event.workflow_id,
                "task_id": event.task_id,
                "task_name": event.task_name,
                "status": event.status,
            },
        )

    if isinstance(event, WorkflowCompletedEvent):
        return WireFrame(
            "workflow_completed",
            {
                "workflow_id": event.workflow_id,
                "goal": event.goal,
                "success": event.success,
                "completed_count": event.completed_count,
                "failed_count": event.failed_count,
                "skipped_count": event.skipped_count,
                "total_duration": event.total_duration,
            },
        )

    if isinstance(event, EngineInstallRequest):
        payload: dict[str, Any] = {
            "request_id": event.request_id,
            "engine": event.engine,
            "title": event.title,
            "body": event.body,
            "hint": event.hint,
            "options": list(event.options),
        }
        # The i18n code fields are optional: omit them entirely when unset so a
        # client never has to distinguish "" from absent.
        if event.title_code:
            payload["title_code"] = event.title_code
            payload["title_args"] = dict(event.title_args)
        if event.body_code:
            payload["body_code"] = event.body_code
            payload["body_args"] = dict(event.body_args)
        if event.hint_code:
            payload["hint_code"] = event.hint_code
            payload["hint_args"] = dict(event.hint_args)
        return WireFrame("engine_install_request", payload)

    if isinstance(event, EngineInstallProgress):
        return WireFrame(
            "engine_install_progress",
            {
                "request_id": event.request_id,
                "pct": event.pct,
                "message": event.message,
                "sub_message": event.sub_message,
                "indeterminate": event.indeterminate,
                "status": event.status,
            },
        )

    if isinstance(event, Finish):
        last_msg_id = None
        if snapshot is not None and snapshot.tail_message is not None:
            tail = snapshot.tail_message
            if tail.get("role") == "assistant":
                last_msg_id = tail.get("id")
        # The compacted message list is resent only when a compaction actually
        # happened during this turn; otherwise the client already has it.
        messages = snapshot.renderer_messages if (event.compacted and snapshot) else []
        return WireFrame(
            "finish",
            {
                "reason": event.reason,
                "usage": event.usage,
                "context_usage": event.context_usage,
                "error": event.error,
                "error_code": event.error_code or "",
                "error_category": event.error_category or "",
                "assistant_message_id": last_msg_id,
                "compacted": event.compacted,
                "messages": messages if messages else None,
                "plan_items": snapshot.plan_items if snapshot else None,
                "artifacts": snapshot.artifacts if snapshot else None,
                "references": snapshot.references if snapshot else None,
            },
        )

    return None


def _project_system_message(event: SystemMessage) -> WireFrame | None:
    """Route a :class:`SystemMessage` to its real frame.

    A ``SystemMessage`` is the loop's escape hatch for anything that must be
    visible in the conversation but is not assistant speech.  Two payloads are
    not ordinary notices and must never render as a "System message" bubble:

    * ``__spec_data__:<json>`` -- a generated specification, re-routed as
      ``spec_update`` so the client renders the review card;
    * ``__plan_review__:<json>`` -- a plan review, re-routed as
      ``plan_review``.

    A malformed payload of either kind yields ``None`` (nothing is sent)
    rather than leaking the raw JSON into the conversation.

    Args:
        event: The system message to route.

    Returns:
        The projected frame, or ``None`` when the payload could not be parsed.
    """
    content = event.content or ""
    if content.startswith(SPEC_DATA_PREFIX):
        try:
            spec_data = json.loads(content[len(SPEC_DATA_PREFIX):])
        except Exception:
            logger.warning(
                "[spec] failed to relay spec_update from system message",
                exc_info=True,
            )
            return None
        return WireFrame("spec_update", {"spec": spec_data, "status": "review"})

    if content.startswith(PLAN_REVIEW_PREFIX):
        try:
            review_data = json.loads(content[len(PLAN_REVIEW_PREFIX):])
        except Exception:
            logger.warning(
                "[plan] failed to relay plan_review from system message",
                exc_info=True,
            )
            return None
        return WireFrame(
            "plan_review",
            {"review": review_data, "status": review_data.get("status") or "review"},
        )

    return WireFrame("system_message", {"content": content, "kind": event.kind})


__all__ = [
    "PLAN_REVIEW_PREFIX",
    "SPEC_DATA_PREFIX",
    "TOOL_RESULT_TRUNCATION_SUFFIX",
    "TOOL_RESULT_WIRE_LIMIT",
    "SessionSnapshot",
    "SnapshotNeed",
    "WireFrame",
    "project_event",
    "session_snapshot_need",
]
