#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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

from __future__ import annotations

"""AgentEvent -> WebSocket frame bridge (``_dispatch_event``).

The single point where agent loop events become server protocol frames.
Extracted verbatim from ``encre.transport.ws`` (architecture refactor
Stage 3 / Task 5.2); behaviour is unchanged, only the layout moved.
"""

import logging
from typing import Any

from encre.server.session_manager import SessionState
from encre.utils.tokens import count_message_tokens
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
    PermissionRequest,
    PlanUpdate,
    QuestionRequest,
    Reference,
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

logger = logging.getLogger("encre.transport.ws")


class EventDispatchMixin:
    """Translates typed agent events into server protocol frames."""

    async def _dispatch_event(self, ws, _info, event: Any) -> None:
        """Translate one agent :class:`~encre.utils.types.AgentEvent` to WS frames.

        Each branch matches an event type and emits the corresponding server
        protocol message (``text_delta``, ``tool_call_start``, ``finish``,
        ``plan_update``, ``compact``, workflow events, engine-install dialogs,
        ...).  Keeps the canvas/usage panel in sync where relevant.
        """
        sid = _info.session_id if _info else None

        async def _send_agent_state() -> None:
            if _info is None:
                return
            state_mgr = getattr(_info.agent.loop, "_state_mgr", None)
            if state_mgr is None:
                return
            mode = getattr(_info.agent.loop, "mode", None)
            await self._send(
                ws,
                "agent_state",
                state=state_mgr.snapshot(),
                mode=str(mode.value) if mode is not None else "general",
                session_id=sid,
            )

        if isinstance(event, TextDelta) and event.text:
            await self._send(ws, "text_delta", text=event.text, session_id=sid)

        elif isinstance(event, ThinkingDelta) and event.text:
            await self._send(ws, "thinking_delta", text=event.text, session_id=sid)

        elif isinstance(event, ToolCallStart):
            await self._send(ws, "tool_call_start", name=event.name, id=event.id, session_id=sid)

        elif isinstance(event, ToolCallDelta):
            await self._send(ws, "tool_call_delta", id=event.id, key=event.key, value=event.value, session_id=sid)

        elif isinstance(event, ToolCallEnd):
            await self._send(ws, "tool_call_end", id=event.id, session_id=sid)

        elif isinstance(event, ToolProgress):
            await self._send(
                ws,
                "tool_progress",
                id=event.id,
                tool_name=event.tool_name,
                status=event.status,
                sub_agent_messages=event.sub_agent_messages,
                session_id=sid,
            )

        elif isinstance(event, ToolResult):
            content = event.content
            if len(content) > 100000:
                content = content[:100000] + "\n... (truncated)"
            await self._send(
                ws,
                "tool_result",
                id=event.id,
                content=content,
                is_error=event.is_error,
                sub_agent_messages=event.sub_agent_messages,
                sub_agent_session_id=event.sub_agent_session_id,
                session_id=sid,
            )

        elif isinstance(event, PermissionRequest):
            self._manager.set_session_state(sid, SessionState.AWAITING_APPROVAL)
            await self._send(ws, "permission_request", tool_name=event.tool_name, reason=event.reason, session_id=sid)

        elif isinstance(event, QuestionRequest):
            await self._send(ws, "question_request", tool_call_id=event.tool_call_id, questions=event.questions, session_id=sid)

        elif isinstance(event, Artifact):
            await self._send(ws, "artifacts_update", artifacts=[event.artifact], session_id=sid)

        elif isinstance(event, BackendError):
            # A backend yielded a structured error event (e.g. provider returned
            # a non-retryable 400 mid-stream).  Surface it to the UI immediately
            # so the user sees the provider's actual error message instead of a
            # generic "Error 400" once the agent loop finally unwinds.
            # Includes code/category/retryable for structured frontend rendering.
            await self._send(ws, "error",
                message=event.error,
                code=event.code or "backend_error",
                category=event.category or "unknown",
                retryable=event.retryable,
                retry_after=event.retry_after,
                details=event.details or {},
                session_id=sid)

        elif isinstance(event, Reference):
            await self._send(ws, "references_update", references=[event.reference], session_id=sid)

        elif isinstance(event, PlanUpdate):
            await self._send(ws, "plan_update", plan_items=event.plan_items, session_id=sid)
            await _send_agent_state()
            # Persist plan items asynchronously (debounced) so they survive app
            # refresh without blocking the event dispatch / subsequent WS sends.
            if _info is not None:
                _info.agent.session.plan_items = event.plan_items
                self._manager._schedule_save(_info)

        elif isinstance(event, AssistantBoundary):
            await self._send(ws, "assistant_boundary", session_id=sid)

        elif isinstance(event, CompactStarting):
            await self._send(ws, "compact_starting", session_id=sid)

        elif isinstance(event, CompactFailed):
            # The compaction did not produce a result (engine failure or
            # no-op): tell the frontend to dismiss the "compressing"
            # indicator so it cannot get stuck on screen.
            await self._send(ws, "compact_failed",
                reason=event.reason or "", session_id=sid)

        elif isinstance(event, CompactNotification):
            # Send the compacted message list so the frontend's state
            # matches the backend. Without this, the frontend still shows
            # compacted-away messages, leading to "Message not found"
            # errors when the user tries to rollback to them.
            compact_msgs = self._renderer_session_messages(_info.agent.session) if _info else []
            compact_session = _info.agent.session if _info else None
            await self._send(ws, "compact",
                old_count=event.old_count,
                new_count=event.new_count,
                old_tokens=event.old_tokens,
                new_tokens=event.new_tokens,
                messages=compact_msgs,
                plan_items=compact_session.plan_items if compact_session else None,
                artifacts=compact_session.artifacts if compact_session else None,
                references=compact_session.references if compact_session else None,
                session_id=sid)
            await _send_agent_state()
        elif isinstance(event, SystemMessage):
            content = event.content or ""
            # Spec data rides on a SystemMessage with an ``__spec_data__:``
            # prefix (loop.py emits it after parsing a generated spec).
            # The raw JSON must NEVER reach the frontend as a visible
            # "System message" bubble -- it would render as a leaked
            # prompt-like strip at the top of the conversation.  Re-route
            # it as a proper ``spec_update`` event so the frontend renders
            # the spec card (with Approve/Reject) instead.
            if content.startswith("__spec_data__:"):
                try:
                    import json as _json
                    spec_data = _json.loads(content[len("__spec_data__:"):])
                    await self._send(ws, "spec_update",
                                     spec=spec_data,
                                     status="review",
                                     session_id=sid)
                except Exception:
                    logger.warning("[spec] failed to relay spec_update from system message", exc_info=True)
            elif content.startswith("__plan_review__:"):
                try:
                    import json as _json
                    review_data = _json.loads(content[len("__plan_review__:"):])
                    await self._send(ws, "plan_review",
                                     review=review_data,
                                     status="review",
                                     session_id=sid)
                except Exception:
                    logger.warning("[plan] failed to relay plan_review from system message", exc_info=True)
            else:
                await self._send(ws, "system_message",
                                content=content,
                                kind=event.kind,
                                session_id=sid)
            # Also push updated context usage to the canvas panel
            if _info is not None:
                ctx_msgs = _info.agent.session.get_context_messages()
                ctx_tokens = count_message_tokens(ctx_msgs)
                window = _info.agent.loop.backend.context_window_size() if _info.agent.loop.backend else 0
                await self._send(ws, "context_usage",
                    context_tokens=ctx_tokens,
                    context_window=window,
                    session_id=sid)

        elif isinstance(event, WorkflowStartedEvent):
            await self._send(ws, "workflow_started",
                workflow_id=event.workflow_id,
                goal=event.goal,
                total_tasks=event.total_tasks,
                task_ids=event.task_ids,
                session_id=sid)

        elif isinstance(event, WorkflowTaskEvent):
            await self._send(ws, "workflow_task",
                workflow_id=event.workflow_id,
                task_id=event.task_id,
                task_name=event.task_name,
                status=event.status,
                session_id=sid)

        elif isinstance(event, WorkflowCompletedEvent):
            await self._send(ws, "workflow_completed",
                workflow_id=event.workflow_id,
                goal=event.goal,
                success=event.success,
                completed_count=event.completed_count,
                failed_count=event.failed_count,
                skipped_count=event.skipped_count,
                total_duration=event.total_duration,
                session_id=sid)

        elif isinstance(event, EngineInstallRequest):
            kwargs: dict[str, Any] = dict(
                request_id=event.request_id,
                engine=event.engine,
                title=event.title,
                body=event.body,
                hint=event.hint,
                options=list(event.options),
                session_id=sid,
            )
            if event.title_code:
                kwargs["title_code"] = event.title_code
                kwargs["title_args"] = dict(event.title_args)
            if event.body_code:
                kwargs["body_code"] = event.body_code
                kwargs["body_args"] = dict(event.body_args)
            if event.hint_code:
                kwargs["hint_code"] = event.hint_code
                kwargs["hint_args"] = dict(event.hint_args)
            await self._send(ws, "engine_install_request", **kwargs)

        elif isinstance(event, EngineInstallProgress):
            await self._send(ws, "engine_install_progress",
                request_id=event.request_id,
                pct=event.pct,
                message=event.message,
                sub_message=event.sub_message,
                indeterminate=event.indeterminate,
                status=event.status,
                session_id=sid)

        elif isinstance(event, Finish):
            # Send the last assistant message ID so the frontend can store it for retry matching
            last_msg_id = None
            if _info and _info.agent.session.messages:
                last = _info.agent.session.messages[-1]
                if last.get("role") == "assistant":
                    last_msg_id = last.get("id")
            # If compaction occurred during this turn, send the updated
            # message list so the frontend's state matches the backend.
            # Without this, compacted-away messages stay in the frontend
            # and cause "Message not found" errors on rollback.
            finish_msgs = []
            if event.compacted and _info:
                finish_msgs = self._renderer_session_messages(_info.agent.session)
            finish_session = _info.agent.session if _info else None
            await self._send(ws, "finish", reason=event.reason, usage=event.usage,
                             error=event.error,
                             error_code=event.error_code or "",
                             error_category=event.error_category or "",
                             assistant_message_id=last_msg_id,
                             compacted=event.compacted,
                             messages=finish_msgs if finish_msgs else None,
                             plan_items=finish_session.plan_items if finish_session else None,
                             artifacts=finish_session.artifacts if finish_session else None,
                             references=finish_session.references if finish_session else None,
                             session_id=sid)
            await _send_agent_state()
            # Push updated context usage so the canvas panel stays in sync
            if _info is not None:
                ctx_msgs = _info.agent.session.get_context_messages()
                ctx_tokens = count_message_tokens(ctx_msgs)
                window = _info.agent.loop.backend.context_window_size() if _info.agent.loop.backend else 0
                await self._send(ws, "context_usage",
                    context_tokens=ctx_tokens,
                    context_window=window,
                    session_id=sid)
