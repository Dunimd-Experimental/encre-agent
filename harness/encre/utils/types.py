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

"""Shared dataclass and type definitions for the Encre agent/backend.

This module is the single source of truth for the value types passed between
the agent loop, the backend adapters and the frontend:

* :data:`FinishReason`, :data:`PermissionMode`, :data:`PermissionBehavior`,
  :data:`TaskType`, :data:`TaskStatus` -- string-literal enums.
* The ``AgentEvent`` union -- every event the agent may stream to the UI
  (text deltas, tool calls, permissions, plan proposals, workflow progress,
  ...).
* The ``BackendEvent`` union -- the normalised events returned by a backend
  adapter.
* The ``ContentBlock`` union -- multimodal message content (text / image /
  file / tool use / tool result).
* Permission decisions, thinking configs, branch-protocol events and the
  provider capability result wrappers (images, audio, embeddings, ...).

Prefer the ``create_*`` factory functions at the bottom of the module over
constructing the dataclasses directly so call sites stay terse.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Union

# 鈹€鈹€ String-literal enumerations 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# Why Literal unions instead of Enum: these values are (de)serialised to/from
# JSON across the agent<->backend<->UI boundary, so plain strings avoid any
# encoding/decoding ceremony.

# How a model turn ended.
FinishReason = Literal["stop", "tool_calls", "error", "max_tokens", "cancelled"]
# Frontend permission mode selected for the session.
PermissionMode = Literal["default", "accept_edits", "bypass", "dont_ask", "plan", "spec", "auto", "blacklist", "custom"]
# Outcome of a single permission decision.
PermissionBehavior = Literal["allow", "deny", "ask"]
# Kind of sub-task dispatched to a worker.
TaskType = Literal["bash", "agent", "workflow"]
# Lifecycle state of a (sub-)task.
TaskStatus = Literal["pending", "running", "completed", "failed", "killed"]


@dataclass
class TextDelta:
    """A chunk of streamed assistant text output."""
    text: str


@dataclass
class ThinkingDelta:
    """A chunk of streamed model reasoning / "thinking" text."""
    text: str


@dataclass
class ToolCallStart:
    """Marks the beginning of a tool invocation (name + unique id)."""
    name: str
    id: str


@dataclass
class ToolCallDelta:
    """One key/value fragment of a tool call's streaming arguments."""
    id: str
    key: str
    value: str


@dataclass
class ToolCallEnd:
    """Marks the end of a streamed tool call identified by *id*."""
    id: str


@dataclass
class ToolProgress:
    """Progress/status update for a long-running tool (or sub-agent)."""
    id: str
    tool_name: str
    status: str
    sub_agent_messages: list[dict[str, Any]] | None = None


@dataclass
class ToolResult:
    """Final result of a tool call, with an error flag and sub-agent context."""
    id: str
    content: str
    is_error: bool
    error_code: str = ""
    error_category: str = ""
    sub_agent_messages: list[dict[str, Any]] | None = None
    sub_agent_session_id: str | None = None


@dataclass
class PermissionRequest:
    """Asks the frontend/user whether a tool may run and why."""
    tool_name: str
    reason: str


@dataclass
class EngineInstallRequest:
    """Sent to frontend when a tool needs a missing engine / driver
    (Playwright bundled Chromium, Edge CDP, msedgedriver, etc.) and
    the LLM should NOT be involved in resolving the choice.

    The frontend is expected to show a native dialog (e.g. Electron
    confirmInstall) and send back an :class:`EngineInstallResponse`
    on the same channel.  The agent run() that yielded this event
    is suspended until the response arrives or the request times out.
    """
    request_id: str
    engine: str  # e.g. "chromium-cdp", "msedgedriver", "edge-cdp"
    title: str
    body: str
    hint: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)
    # Each option is ``{"id": str, "label": str, "description": str,
    # "kind": "primary"|"secondary"}``.  The frontend should default to
    # the first option on Enter.
    # I18n: when these message_code fields are non-empty the frontend
    # resolves the text via t() instead of using the raw string.
    title_code: str = ""
    title_args: dict[str, str] = field(default_factory=dict)
    body_code: str = ""
    body_args: dict[str, str] = field(default_factory=dict)
    hint_code: str = ""
    hint_args: dict[str, str] = field(default_factory=dict)


@dataclass
class EngineInstallProgress:
    """Progress update for an in-flight engine install / connect."""
    request_id: str
    pct: float  # 0..100
    message: str
    sub_message: str = ""
    indeterminate: bool = False
    status: str = "running"  # "running" | "success" | "fail" | "cancelled"
    # I18n: backend sends a message_code instead of a hardcoded string;
    # frontend looks up the translation.  message_args provides template
    # variables (e.g. ``{"pct": "42"}``).
    message_code: str = ""
    message_args: dict[str, str] = field(default_factory=dict)
    sub_message_code: str = ""
    sub_message_args: dict[str, str] = field(default_factory=dict)


AgentEvent = Union[
    "TextDelta", "ThinkingDelta",
    "ToolCallStart", "ToolCallDelta", "ToolCallEnd",
    "ToolProgress", "ToolResult",
    "PermissionRequest", "QuestionRequest",
    "EngineInstallRequest", "EngineInstallProgress",
    "Finish",
]


@dataclass
class QuestionRequest:
    """Sent to frontend when the model asks the user questions."""
    tool_call_id: str
    questions: list[dict[str, Any]]
    """Each dict: {"question": str, "details"?: str, "options"?: [str]}"""


@dataclass
class Finish:
    reason: FinishReason
    usage: dict[str, Any] | None = None
    error: str | None = None
    error_code: str = ""
    error_category: str = ""
    compacted: bool = False


@dataclass
class Artifact:
    """A structured artifact produced by the agent (attached file/resource)."""
    artifact: dict[str, Any]


@dataclass
class Reference:
    """A reference (link/citation) surfaced by the agent."""
    reference: dict[str, Any]


@dataclass
class PlanUpdate:
    """Incremental update to the agent's plan/outline items."""
    plan_items: list[dict[str, Any]]


@dataclass
class PlanProposal:
    """A pending write action the agent wants to perform in plan mode.

    The desktop UI shows the preview/diff and lets the user approve or
    reject. The agent only executes the underlying tool when the user
    explicitly approves.

    Attributes:
        proposal_id: Stable id so the UI can route approve/reject back.
        tool_call_id: Originating tool call id from the model.
        tool_name: Concrete tool that would run (file_write, file_edit,
            apply_patch, bash, etc.).
        tool_args: The original args the model produced.
        preview: A short human-readable description of the intended
            change (e.g. ``"Modify backend/encre/loop.py: +12 -4"``).
        diff_text: Optional unified diff for file-shaped proposals.
        file_path: Optional file path the proposal targets.
        original: Optional original file content (for diff display).
        proposed: Optional proposed file content.
        added: Lines added (for diff display).
        removed: Lines removed (for diff display).
        risk: Optional risk hint (``"low" | "medium" | "high"``).
    """

    proposal_id: str
    tool_call_id: str
    tool_name: str
    tool_args: dict[str, Any] = field(default_factory=dict)
    preview: str = ""
    diff_text: str = ""
    file_path: str = ""
    original: str = ""
    proposed: str = ""
    added: int = 0
    removed: int = 0
    risk: str = "low"


@dataclass
class PlanModeChanged:
    """Emitted when the agent enters or exits plan mode."""

    active: bool
    reason: str = ""


@dataclass
class PlanResolved:
    """Emitted when the user resolves a pending plan proposal."""

    proposal_id: str
    tool_call_id: str
    approved: bool


@dataclass
class PlanReviewData:
    """A plan/spec document submitted for user review (saved as 3 markdown files)."""

    review_id: str
    content: str
    file_path: str  # path to plan.md
    dir_path: str   # directory containing plan.md, steps.md, checklist.md
    mode: str  # "plan" | "spec"
    status: str  # "review" | "approved" | "rejected"


@dataclass
class CompactStarting:
    """Signals that a context compaction is about to begin.

    The frontend should show a non-expandable compression indicator
    while the model pauses to compact the context.
    """


@dataclass
class CompactFailed:
    """Signals that a compaction did NOT produce a compacted result.

    Emitted when the compact engine raised, or returned ``None`` (no-op),
    after a ``CompactStarting`` was already sent.  The frontend must dismiss
    the "compressing" indicator so it cannot get stuck on screen; the
    conversation simply continues with the uncompressed context.
    """

    reason: str = ""


@dataclass
class CompactNotification:
    """Reports the before/after message and token counts of a compaction."""
    old_count: int
    new_count: int
    old_tokens: int
    new_tokens: int


@dataclass
class SystemMessage:
    """A system-level notification to be rendered in the conversation.

    Used for model fallback announcements, slot escalation hints,
    max-tokens recovery messages, and other non-error system events
    that should be visible to the user but not attributed to the
    assistant.
    """
    content: str
    kind: str = "info"  # "info" | "warning" | "recovery"


@dataclass
class EditProposal:
    """A pending file edit the agent proposes but has not yet applied.

    Emitted from the agent loop (typically by the ``file_edit`` tool
    with ``dry_run=True``) and consumed by the desktop UI which
    renders an inline diff and lets the user accept or reject the
    change.  The agent does not write the file until the user accepts.

    Attributes:
        tool_call_id: The originating tool call id -- required so the
            UI can route the user's accept/reject decision back to
            the correct pending request.
        file_path: Absolute path of the file the edit applies to.
        diff_text: Unified diff text (Rust-native ``compute_diff``
            output) for rendering in the UI.
        original: The file content **before** the proposed edit.
        proposed: The file content **after** the proposed edit.
        added: Number of inserted lines.
        removed: Number of deleted lines.
        summary: Optional human-readable summary of the edits
            (e.g. ``"renamed foo to bar in module X"``).
    """

    tool_call_id: str
    file_path: str
    diff_text: str
    original: str
    proposed: str
    added: int
    removed: int
    summary: str = ""


def create_edit_proposal(
    tool_call_id: str,
    file_path: str,
    diff_text: str,
    original: str,
    proposed: str,
    added: int,
    removed: int,
    summary: str = "",
) -> EditProposal:
    return EditProposal(
        tool_call_id=tool_call_id,
        file_path=file_path,
        diff_text=diff_text,
        original=original,
        proposed=proposed,
        added=added,
        removed=removed,
        summary=summary,
    )


@dataclass
class AssistantBoundary:
    """Marker event separating two assistant turns (no payload)."""
    pass


@dataclass
class WorkflowStartedEvent:
    """Emitted when a multi-task workflow begins executing."""
    workflow_id: str
    goal: str
    total_tasks: int
    task_ids: list[str] = field(default_factory=list)


@dataclass
class WorkflowTaskEvent:
    """Per-task status change inside a running workflow."""
    workflow_id: str
    task_id: str
    task_name: str
    status: str  # started | running | completed | failed | skipped


@dataclass
class WorkflowCompletedEvent:
    """Summary emitted when a workflow finishes (success + counts + duration)."""
    workflow_id: str
    goal: str
    success: bool
    completed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    total_duration: float = 0.0


AgentEvent = TextDelta | ThinkingDelta | ToolCallStart | ToolCallDelta | ToolCallEnd | ToolProgress | ToolResult | PermissionRequest | QuestionRequest | Finish | Artifact | PlanUpdate | CompactStarting | CompactNotification | EditProposal | AssistantBoundary | WorkflowStartedEvent | WorkflowTaskEvent | WorkflowCompletedEvent | SystemMessage


# 鈹€鈹€ Multimodal Content Blocks 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


@dataclass
class TextContent:
    """A plain text content block for multimodal messages."""
    type: str = "text"
    text: str = ""


@dataclass
class ImageContent:
    """An inline image (base64 data or remote URL) content block."""
    type: str = "image"
    data: str = ""           # base64 encoded
    mime_type: str = "image/png"
    source_url: str = ""


@dataclass
class FileContent:
    """An inline file (base64) content block."""
    type: str = "file"
    data: str = ""           # base64 encoded
    filename: str = ""
    mime_type: str = "application/octet-stream"


@dataclass
class ToolUseContent:
    """A tool invocation request carried inside a content block."""
    type: str = "tool_use"
    id: str = ""
    name: str = ""
    input: dict[str, Any] | None = None


@dataclass
class ToolResultContent:
    """The result of a tool use, embedded as a content block."""
    type: str = "tool_result"
    tool_use_id: str = ""
    content: str = ""
    is_error: bool = False


# Union of every message-content shape the agent can send/receive.
ContentBlock = TextContent | ImageContent | FileContent | ToolUseContent | ToolResultContent


@dataclass
class BackendText:
    """Normalised text chunk emitted by a backend adapter."""
    text: str


@dataclass
class BackendThinking:
    """Normalised reasoning chunk emitted by a backend adapter."""
    text: str
    signature_delta: str | None = None


@dataclass
class BackendToolCall:
    """Normalised tool call (full arguments) from a backend adapter."""
    id: str
    name: str
    arguments: str


@dataclass
class BackendToolCallDelta:
    """Normalised streaming fragment of a backend tool call."""
    index: int
    key: str
    value: str


@dataclass
class BackendFinish:
    """Normalised finish event from a backend adapter."""
    reason: str
    usage: dict[str, Any] | None = None


@dataclass
class BackendError:
    """Normalised, code-classified error event from a backend adapter."""
    error: str
    code: str = "unknown"
    category: str = "unknown"
    retryable: bool = False
    retry_after: float | None = None
    details: dict[str, Any] | None = None


# Union of every normalised event a backend adapter may emit.
BackendEvent = BackendText | BackendThinking | BackendToolCall | BackendToolCallDelta | BackendFinish | BackendError


@dataclass
class PermissionAllow:
    """Decision: permit the tool call to run."""
    behavior: PermissionBehavior = "allow"
    reason: str = ""


@dataclass
class PermissionDeny:
    """Decision: reject the tool call."""
    behavior: PermissionBehavior = "deny"
    reason: str = ""


@dataclass
class PermissionAsk:
    """Decision: ask the user before running, bound to a rule id."""
    behavior: PermissionBehavior = "ask"
    reason: str = ""
    rule: str = ""


# One of the three possible permission outcomes.
PermissionDecision = PermissionAllow | PermissionDeny | PermissionAsk


@dataclass
class AdaptiveThinking:
    """Budgeted "thinking" that scales with the model's token budget."""
    enabled: bool = True
    min_tokens: int = 1024
    max_tokens: int = 8192
    budget_ratio: float = 0.5
    level: str = ""  # "low", "medium", "high", "max" (backend-specific)


@dataclass
class EnabledThinking:
    """Fixed-budget thinking mode."""
    enabled: bool = True
    budget_tokens: int = 4096
    level: str = ""  # "low", "medium", "high", "max" (backend-specific)
    selectable: bool = True  # whether the user can change the level in the UI


@dataclass
class DisabledThinking:
    """Thinking turned off entirely."""
    enabled: bool = False
    level: str = ""


# Selectable thinking/Reasoning configuration variants.
ThinkingConfig = AdaptiveThinking | EnabledThinking | DisabledThinking


def create_text_delta(text: str) -> TextDelta:
    return TextDelta(text=text)


def create_thinking_delta(text: str) -> ThinkingDelta:
    return ThinkingDelta(text=text)


def create_tool_call_start(name: str, id: str) -> ToolCallStart:
    return ToolCallStart(name=name, id=id)


def create_tool_call_delta(id: str, key: str, value: str) -> ToolCallDelta:
    return ToolCallDelta(id=id, key=key, value=value)


def create_tool_call_end(id: str) -> ToolCallEnd:
    return ToolCallEnd(id=id)


def create_tool_progress(
    id: str,
    tool_name: str,
    status: str,
    sub_agent_messages: list[dict[str, Any]] | None = None,
) -> ToolProgress:
    return ToolProgress(
        id=id,
        tool_name=tool_name,
        status=status,
        sub_agent_messages=sub_agent_messages,
    )


def create_tool_result(
    id: str,
    content: str,
    is_error: bool = False,
    sub_agent_messages: list[dict[str, Any]] | None = None,
    sub_agent_session_id: str | None = None,
) -> ToolResult:
    return ToolResult(
        id=id,
        content=content,
        is_error=is_error,
        sub_agent_messages=sub_agent_messages,
        sub_agent_session_id=sub_agent_session_id,
    )


def create_permission_request(tool_name: str, reason: str) -> PermissionRequest:
    return PermissionRequest(tool_name=tool_name, reason=reason)


def create_question_request(tool_call_id: str, questions: list[dict[str, Any]]) -> QuestionRequest:
    return QuestionRequest(tool_call_id=tool_call_id, questions=questions)


def create_finish(reason: FinishReason, usage: dict[str, Any] | None = None, error: str | None = None,
                   error_code: str = "", error_category: str = "") -> Finish:
    return Finish(reason=reason, usage=usage, error=error,
                  error_code=error_code, error_category=error_category)


def create_system_message(content: str, kind: str = "info") -> SystemMessage:
    return SystemMessage(content=content, kind=kind)


def create_backend_text(text: str) -> BackendText:
    return BackendText(text=text)


def create_backend_thinking(text: str, signature_delta: str | None = None) -> BackendThinking:
    return BackendThinking(text=text, signature_delta=signature_delta)


def create_backend_tool_call(id: str, name: str, arguments: str) -> BackendToolCall:
    return BackendToolCall(id=id, name=name, arguments=arguments)


def create_backend_tool_call_delta(index: int, key: str, value: str) -> BackendToolCallDelta:
    return BackendToolCallDelta(index=index, key=key, value=value)


def create_backend_finish(reason: str, usage: dict[str, Any] | None = None) -> BackendFinish:
    return BackendFinish(reason=reason, usage=usage)


def create_backend_error(error: str, code: str = "unknown", category: str = "unknown",
                          retryable: bool = False, retry_after: float | None = None,
                          details: dict[str, Any] | None = None) -> BackendError:
    return BackendError(error=error, code=code, category=category,
                         retryable=retryable, retry_after=retry_after,
                         details=details)


def create_artifact(artifact: dict[str, Any]) -> Artifact:
    return Artifact(artifact=artifact)


def create_assistant_boundary() -> AssistantBoundary:
    return AssistantBoundary()


def create_plan_proposal(
    proposal_id: str,
    tool_call_id: str,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
    preview: str = "",
    diff_text: str = "",
    file_path: str = "",
    original: str = "",
    proposed: str = "",
    added: int = 0,
    removed: int = 0,
    risk: str = "low",
) -> PlanProposal:
    return PlanProposal(
        proposal_id=proposal_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        tool_args=tool_args or {},
        preview=preview,
        diff_text=diff_text,
        file_path=file_path,
        original=original,
        proposed=proposed,
        added=added,
        removed=removed,
        risk=risk,
    )


def create_plan_mode_changed(active: bool, reason: str = "") -> PlanModeChanged:
    return PlanModeChanged(active=active, reason=reason)


def create_plan_resolved(proposal_id: str, tool_call_id: str, approved: bool) -> PlanResolved:
    return PlanResolved(proposal_id=proposal_id, tool_call_id=tool_call_id, approved=approved)


def create_plan_review_data(
    review_id: str,
    content: str,
    file_path: str,
    dir_path: str = "",
    mode: str = "plan",
    status: str = "review",
) -> PlanReviewData:
    return PlanReviewData(
        review_id=review_id,
        content=content,
        file_path=file_path,
        dir_path=dir_path,
        mode=mode,
        status=status,
    )


# 鈹€鈹€ Branch Protocol Types 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


@dataclass
class BranchMetaData:
    id: str
    parent_branch_id: str | None = None
    fork_point_message_id: str | None = None
    created_at: float = 0.0
    messages_count: int = 0
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0, "total": 0})


@dataclass
class BranchUpdated:
    active_branch_id: str
    branches: list[dict[str, Any]]
    messages: list[dict[str, Any]]


@dataclass
class BranchSwitched:
    branch_id: str
    messages: list[dict[str, Any]]
    branches: list[dict[str, Any]]
    tokens: dict[str, int] | None = None


@dataclass
class BranchRolledBack:
    branch_id: str
    removed_message_ids: list[str]


# Union of every branch-protocol event emitted by the session store.
BranchEvent = BranchUpdated | BranchSwitched | BranchRolledBack


# 鈹€鈹€ Multimodal Capability Result Types 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# These result types are returned by the multimodal methods declared on
# :class:`encre.backends.base.BaseBackend`.  They are intentionally
# self-contained (no streaming / delta variants) because each capability
# corresponds to a discrete provider call.


@dataclass
class ImageResult:
    """A single generated or edited image.

    Providers that return a URL place it in :attr:`url`.  Providers that
    return raw bytes (DALL-E ``b64_json``, Bedrock Stability, Imagen)
    place them base64-encoded in :attr:`b64_json`.  Consumers should
    always check both fields.
    """

    url: str = ""
    b64_json: str = ""
    revised_prompt: str = ""
    mime_type: str = "image/png"
    width: int | None = None
    height: int | None = None
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageGenerationResponse:
    """Response wrapper for image generation / edit calls.

    Aligns with the OpenAI Images API envelope so that callers can rely on
    the same shape regardless of provider.
    """

    created: int = 0
    data: list[ImageResult] = field(default_factory=list)
    provider: str = ""
    model: str = ""


@dataclass
class AudioResult:
    """A single generated speech or transcribed text.

    For TTS: ``audio_b64`` contains the synthesised audio as base64 and
    ``audio_format`` identifies the container (``mp3``, ``opus``, ``wav``).

    For STT/Translation: ``text`` contains the transcribed/translated text
    and ``segments`` (if present) holds the timed segments.
    """

    audio_b64: str = ""
    audio_format: str = "mp3"
    text: str = ""
    language: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    duration: float | None = None
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EmbeddingResult:
    """A single embedding vector plus its provenance."""

    index: int = 0
    embedding: list[float] = field(default_factory=list)
    object: str = "embedding"
    model: str = ""


@dataclass
class EmbeddingResponse:
    """Embedding response wrapper matching the OpenAI embeddings API."""

    object: str = "list"
    data: list[EmbeddingResult] = field(default_factory=list)
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    provider: str = ""


@dataclass
class ModerationCategory:
    """A single moderation category score."""

    name: str = ""
    score: float = 0.0
    flagged: bool = False


@dataclass
class ModerationResult:
    """Moderation classification result for a single input."""

    flagged: bool = False
    categories: list[ModerationCategory] = field(default_factory=list)
    category_scores: dict[str, float] = field(default_factory=dict)
    input_text: str = ""


@dataclass
class ModerationResponse:
    """Moderation response wrapper matching the OpenAI moderations API."""

    id: str = ""
    model: str = ""
    results: list[ModerationResult] = field(default_factory=list)
    provider: str = ""


@dataclass
class FileObject:
    """Metadata for a provider-managed file (uploaded / staged)."""

    id: str = ""
    object: str = "file"
    bytes: int = 0
    created_at: int = 0
    filename: str = ""
    purpose: str = ""
    status: str = ""
    mime_type: str = ""
    provider: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FileListResponse:
    """Listing response for provider file APIs."""

    object: str = "list"
    data: list[FileObject] = field(default_factory=list)
    has_more: bool = False


@dataclass
class FileContent:
    """Raw content of a downloaded provider file."""

    file_id: str = ""
    filename: str = ""
    content_b64: str = ""
    mime_type: str = ""
    provider: str = ""


@dataclass
class BatchRequest:
    """Descriptor for a single request inside a provider batch."""

    custom_id: str = ""
    method: str = "POST"
    url: str = ""
    body: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchObject:
    """Provider batch job metadata."""

    id: str = ""
    object: str = "batch"
    status: str = ""
    endpoint: str = ""
    input_file_id: str = ""
    completion_window: str = ""
    created_at: int = 0
    expires_at: int = 0
    completed_at: int | None = None
    failed_at: int | None = None
    request_counts: dict[str, int] = field(default_factory=dict)
    output_file_id: str = ""
    error_file_id: str = ""
    provider: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchListResponse:
    """Listing response for provider batch APIs."""

    object: str = "list"
    data: list[BatchObject] = field(default_factory=list)
    has_more: bool = False


@dataclass
class FineTuneHyperparameters:
    """Hyperparameters for a fine-tuning job."""

    n_epochs: int | str | None = "auto"
    batch_size: int | str | None = "auto"
    learning_rate_multiplier: float | str | None = "auto"


@dataclass
class FineTuneJob:
    """Provider fine-tuning job metadata."""

    id: str = ""
    object: str = "fine_tuning.job"
    model: str = ""
    created_at: int = 0
    finished_at: int | None = None
    fine_tuned_model: str = ""
    status: str = ""
    training_file: str = ""
    validation_file: str = ""
    hyperparameters: dict[str, Any] = field(default_factory=dict)
    trained_tokens: int | None = None
    error: dict[str, Any] = field(default_factory=dict)
    provider: str = ""


@dataclass
class FineTuneEvent:
    """Single event from a fine-tuning job's event log."""

    id: str = ""
    created_at: int = 0
    level: str = "info"
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class FineTuneJobList:
    """Listing response for provider fine-tuning APIs."""

    object: str = "list"
    data: list[FineTuneJob] = field(default_factory=list)
    has_more: bool = False


@dataclass
class RealtimeSessionConfig:
    """Configuration for an OpenAI Realtime WebSocket session."""

    model: str = ""
    voice: str = "alloy"
    modalities: list[str] = field(default_factory=lambda: ["text", "audio"])
    instructions: str = ""
    input_audio_format: str = "pcm16"
    output_audio_format: str = "pcm16"
    temperature: float = 0.8
    max_response_output_tokens: int | str = "inf"
    turn_detection: dict[str, Any] | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_choice: str = "auto"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RealtimeSession:
    """Handle to an active Realtime session.

    Holds the provider session id, the negotiated model, and an opaque
    ``transport`` object (typically an ``httpx_ws`` websocket) that the
    caller can use to send / receive frames.
    """

    session_id: str = ""
    model: str = ""
    expires_at: int = 0
    transport: Any = None
    provider: str = ""


@dataclass
class ResponseObject:
    """OpenAI Responses API response object.

    The Responses API unifies chat, tool calling, structured outputs and
    built-in tools under a single endpoint.  We expose both the raw
    ``raw`` payload and a normalised convenience view.
    """

    id: str = ""
    object: str = "response"
    created_at: int = 0
    status: str = ""
    output: list[dict[str, Any]] = field(default_factory=list)
    output_text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    error: dict[str, Any] | None = None
    provider: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# Union of every provider capability result wrapper exposed by the backend.
BackendMultimodalResult = (
    ImageGenerationResponse
    | AudioResult
    | EmbeddingResponse
    | ModerationResponse
    | FileObject
    | FileListResponse
    | FileContent
    | BatchObject
    | BatchListResponse
    | FineTuneJob
    | FineTuneEvent
    | FineTuneJobList
    | RealtimeSession
    | ResponseObject
)
