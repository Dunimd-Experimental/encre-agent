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

"""Encre WebSocket protocol: typed client/server messages.

This module defines the wire format exchanged between the desktop client and the
:mod:`encre.transport.ws` handler.  Client messages are dataclasses
(``Client*``) with a ``from_dict`` constructor; server messages are produced by
the ``encode_*`` convenience functions, which optionally AES-encrypt the JSON
payload.

Security note: sensitive client/server payloads may be AES-256-GCM encrypted
(see :func:`_parse_client_encrypted` / :func:`encode_server_encrypted`).  A
plaintext ``{...}`` payload is treated as a legacy/backwards-compatible message.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from encre.crypto import decrypt, encrypt

# Wire protocol version.  Clients declare their version on any frame
# (``protocol_version`` JSON field, typically the first); the server answers
# with ``protocol_version`` piggybacked on the first ``session_ready`` frame.
# Old clients that never declare a version stay compatible: the server treats
# them as the current version.  A client newer than the server logs a warning
# but is not rejected (additive fields are ignored by the older peer).
PROTOCOL_VERSION = 1

# 鈹€鈹€ Transport鈥憀ayer encryption wrappers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def _parse_client_encrypted(raw: str | bytes) -> str | bytes | None:
    """Decrypt an encrypted client message, returning the raw JSON string.

    If the payload is valid base64 ciphertext, decrypt it.  Otherwise
    treat the message as legacy plaintext (backwards鈥慶ompatible).
    """
    if not raw:
        return raw
    try:
        decoded = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError:
        return raw  # invalid UTF-8 鈥?return as-is for caller to handle

    # A valid base64 ciphertext from AES鈥慓CM is always at least
    # 12 (nonce) + 1 (min ciphertext) + 16 (tag) 鈮?30 bytes 鈮?40 base64 chars.
    # Messages starting with "{" are almost certainly plain JSON.
    stripped = decoded.strip()
    if not stripped:
        return decoded
    if stripped.startswith("{"):
        return decoded  # legacy plaintext

    try:
        return decrypt(stripped)
    except Exception:
        # Decryption failed -- return the raw payload so the upper
        # layer can still attempt to parse it as plaintext (or fail gracefully).
        return decoded


def encode_server_encrypted(msg_type: str, **kwargs: Any) -> str:
    """Encode and encrypt a server鈥憈o鈥慶lient message.

    The message is serialized as JSON, encrypted via AES鈥?56鈥慓CM, and
    returned as a base64 ciphertext string.
    """
    payload = json.dumps({"type": msg_type, **kwargs}, ensure_ascii=False)
    return encrypt(payload)


def encode_server_plaintext(msg_type: str, **kwargs: Any) -> str:
    """Encode a server鈥憈o鈥慶lient message as plain JSON (no encryption).

    Use this when the client does not have crypto capability or when
    transport encryption is not required (e.g. localhost-only deployments).
    """
    return json.dumps({"type": msg_type, **kwargs}, ensure_ascii=False)


# 鈹€鈹€ Client -> Server message types 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

ClientMessageType = Literal[
    "run", "respond_permission", "cancel", "resume", "configure", "ping",
    "list_models", "list_sessions", "new_session",
    "get_config", "update_models", "set_active_model", "delete_model",
    "fetch_models",
    "update_skills", "install_skill", "uninstall_skill", "update_skill", "update_mcp", "update_agent",
    "search",
    "rollback_log", "rollback_checkout",
    "validate_model",
    "edit_message", "delete_message",
    "delete_session", "export_session", "rename_session",
    "archive_session", "unarchive_session", "list_archived_sessions",
    "agent_create", "agent_delete", "agent_update", "agent_list", "agent_set_active",
    "update_sub_agents",
    "get_memory_list",
    "get_memory_detail",
    "get_global_rules",
    "get_global_rule_content",
    "save_global_rule",
    "delete_global_rule",
    "get_profile",
    "reindex_workspace",
    "get_gitignore",
    "delete_index",
    "get_index_status",
    "set_gitignore",
    "terminal_spawn",
    "terminal_write",
    "terminal_resize",
    "terminal_kill",
    "terminal_list_shells",
    "retry",
    "switch_branch",
    "rollback",
    "get_usage_stats",
    "replay_get_session",
    "automation_list_jobs",
    "automation_create_job",
    "automation_cancel_job",
    "automation_get_history",
    "steer",
    "spec_approve",
    "spec_reject",
    "plan_approve",
    "plan_reject",
    "set_mode",
    "browser_cdp_url",
    "export_data",
    "import_data",
]


@dataclass
class ClientSteer:
    """Mid-run instruction injection.

    Unlike ``ClientRun``, this does NOT start a new turn.  Instead it
    queues the instruction into the agent's ``SteerQueue`` so the
    currently-running loop iteration picks it up before the next API call.
    """

    type: str = "steer"
    session_id: str | None = None
    prompt: str = ""


@dataclass
class ClientSpecApprove:
    """Approve the current spec, allowing the agent to proceed with implementation."""

    type: str = "spec_approve"
    session_id: str | None = None


@dataclass
class ClientSpecReject:
    """Reject the current spec with feedback, asking the agent to revise."""

    type: str = "spec_reject"
    session_id: str | None = None
    feedback: str = ""


@dataclass
class ClientPlanApprove:
    """Approve the current plan, allowing the agent to proceed with execution."""

    type: str = "plan_approve"
    session_id: str | None = None
    review_id: str = ""


@dataclass
class ClientPlanReject:
    """Reject the current plan with feedback, asking the agent to revise."""

    type: str = "plan_reject"
    session_id: str | None = None
    review_id: str = ""
    feedback: str = ""


@dataclass
class ClientRun:
    """Request to run a prompt through the agent (the primary user action).

    Carries the prompt, optional system prompt, target session, attachments,
    slash-command mode, and an ``iclaw`` channel flag for iClaw runs.
    ``temp_chat`` marks ephemeral sessions that are never persisted.
    """

    type: str = "run"
    prompt: str = ""
    system_prompt: str | None = None
    session_id: str | None = None
    specialty: str = "general"
    attachments: list[dict[str, Any]] | None = None
    mode: str | None = None
    mode_prompt: str | None = None
    channel: str | None = None  # "iclaw" when in iClaw mode
    temp_chat: bool = False  # True = ephemeral, never persist to disk

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRun":
        return cls(
            type="run",
            prompt=d.get("prompt", ""),
            system_prompt=d.get("system_prompt"),
            session_id=d.get("session_id"),
            specialty=d.get("specialty", "general"),
            attachments=d.get("attachments"),
            mode=d.get("mode"),
            mode_prompt=d.get("mode_prompt"),
            channel=d.get("channel"),
            temp_chat=bool(d.get("temp_chat", False)),
        )


@dataclass
class ClientRespondPermission:
    type: str = "respond_permission"
    tool_name: str = ""
    decision: bool = False
    # When True the decision is persisted into the session policy table so
    # the same tool is not asked again during this session ("always allow").
    remember: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRespondPermission":
        return cls(
            type="respond_permission",
            tool_name=d.get("tool_name", ""),
            decision=d.get("decision", False),
            remember=bool(d.get("remember", False)),
        )


@dataclass
class ClientRespondPlan:
    type: str = "respond_plan"
    proposal_id: str = ""
    approved: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRespondPlan":
        return cls(
            type="respond_plan",
            proposal_id=str(d.get("proposal_id", "")),
            approved=bool(d.get("approved", False)),
        )


@dataclass
class ClientSetPlanMode:
    type: str = "set_plan_mode"
    active: bool = False
    reason: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetPlanMode":
        return cls(
            type="set_plan_mode",
            active=bool(d.get("active", False)),
            reason=str(d.get("reason", "")),
        )


@dataclass
class ClientSetMode:
    """Set or clear the persistent slash-command mode (plan/spec).

    Unlike ``ClientRun.mode`` which is per-run, this persists the mode
    in session metadata so it stays active across multiple runs.
    The frontend sends this when the user clicks the mode chip's X button
    or activates a mode via shortcut.
    """

    type: str = "set_mode"
    session_id: str | None = None
    mode: str = ""  # "plan", "spec", or "" to clear

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetMode":
        return cls(
            type="set_mode",
            session_id=str(d.get("session_id", "")) or None,
            mode=str(d.get("mode", "")),
        )


@dataclass
class ClientExportData:
    """Request a full plaintext zip of the entire data directory.

    The server builds the archive on disk and returns its path (plus progress
    via ``data_export_progress`` events) through a ``data_exported_zip`` event.
    ``request_id`` lets the caller correlate the response with the original
    request.
    """

    type: str = "export_data"
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientExportData":
        return cls(type="export_data", request_id=str(d.get("request_id", "")))


@dataclass
class ClientImportData:
    """Import a plaintext backup zip into this machine's data directory.

    ``zip_path`` is a local filesystem path to the archive produced by
    :func:`encre.migration.export_all` (the desktop renderer passes the path
    the user picked, avoiding a large base64 round-trip over the WebSocket).
    The server re-encrypts with this machine's key and reports via
    ``data_import_progress`` events, then ``data_import_done``.

    ``mode`` controls how imported files interact with data already on this
    machine: ``"overwrite"`` (merge, default), ``"replace"`` (wipe the data dir
    first for a faithful clone) or ``"skip"`` (never overwrite existing files).
    """

    type: str = "import_data"
    zip_path: str = ""
    mode: str = "overwrite"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientImportData":
        return cls(
            type="import_data",
            zip_path=str(d.get("zip_path", "")),
            mode=str(d.get("mode", "overwrite")),
        )


@dataclass
class ClientSetCdpUrl:
    """Set the CDP WebSocket URL for the embedded browser webview."""

    type: str = "browser_cdp_url"
    url: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetCdpUrl":
        return cls(
            type="browser_cdp_url",
            url=str(d.get("url", "")),
            session_id=str(d.get("session_id", "")),
        )


@dataclass
class ClientSetCommand:
    """Set or clear the persistent slash *command* (a sticky prompt injection).

    A command is NOT a mode: it re-injects its prompt body every turn while
    active but triggers no tool interception and no spec gate.  Unlike
    ``ClientRun.mode_prompt`` (one-shot, per-run), this persists the command
    in session metadata so it stays active across runs until cleared.
    The frontend sends this when the user activates a command chip (built-in
    action or user-defined).  An empty ``name`` clears the active command.
    """

    type: str = "set_command"
    session_id: str | None = None
    name: str = ""
    prompt: str = ""  # rendered prompt body ({{args}} already substituted)
    icon: str = ""
    title: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetCommand":
        return cls(
            type="set_command",
            session_id=str(d.get("session_id", "")) or None,
            name=str(d.get("name", "")),
            prompt=str(d.get("prompt", "")),
            icon=str(d.get("icon", "")),
            title=str(d.get("title", "")),
        )


@dataclass
class ClientRespondQuestion:
    type: str = "respond_question"
    tool_call_id: str = ""
    answers: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRespondQuestion":
        return cls(
            type="respond_question",
            tool_call_id=str(d.get("tool_call_id", "")),
            answers=str(d.get("answers", "")),
        )


@dataclass
class ClientEngineInstallResponse:
    type: str = "engine_install_response"
    request_id: str = ""
    choice: str = "cancelled"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientEngineInstallResponse":
        return cls(
            type="engine_install_response",
            request_id=d.get("request_id", ""),
            choice=d.get("choice", "cancelled"),
        )


@dataclass
class ClientCancel:
    type: str = "cancel"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientCancel":
        return cls(
            type="cancel",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientResume:
    type: str = "resume"
    session_id: str = ""
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientResume":
        return cls(
            type="resume",
            session_id=d.get("session_id", ""),
            request_id=d.get("request_id", ""),
        )


@dataclass
class ClientConfigure:
    type: str = "configure"
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientConfigure":
        return cls(
            type="configure",
            config=d.get("config", {}),
        )


@dataclass
class ClientWechatScan:
    type: str = "wechat_scan"
    adapter_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientWechatScan":
        return cls(
            type="wechat_scan",
            adapter_id=d.get("adapter_id", "weixin"),
        )


@dataclass
class ClientPing:
    type: str = "ping"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientPing":
        return cls(type="ping")


@dataclass
class ClientListModels:
    type: str = "list_models"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListModels":
        return cls(type="list_models")


@dataclass
class ClientListSessions:
    type: str = "list_sessions"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListSessions":
        return cls(type="list_sessions")


@dataclass
class ClientListAllSessions:
    """List sessions across ALL channels (normal + iwork), unfiltered.

    Used by the tray popup which needs both mode's sessions at once.
    Returns a ``sessions_all`` event with ``{normal: [...], iwork: [...]}``.
    """

    type: str = "list_all_sessions"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListAllSessions":
        return cls(type="list_all_sessions")


@dataclass
class ClientNewSession:
    type: str = "new_session"
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientNewSession":
        return cls(type="new_session", request_id=d.get("request_id", ""))


@dataclass
class ClientGetConfig:
    type: str = "get_config"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientGetConfig":
        return cls(type="get_config")


@dataclass
class ClientUpdateModels:
    type: str = "update_models"
    models: list[dict[str, Any]] = field(default_factory=list)
    active_model_index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateModels":
        return cls(
            type="update_models",
            models=d.get("models", []),
            active_model_index=d.get("active_model_index", 0),
        )


@dataclass
class ClientSetActiveModel:
    type: str = "set_active_model"
    model_index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetActiveModel":
        return cls(
            type="set_active_model",
            model_index=d.get("model_index", 0),
        )


@dataclass
class ClientDeleteModel:
    type: str = "delete_model"
    model_index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientDeleteModel":
        return cls(
            type="delete_model",
            model_index=d.get("model_index", 0),
        )


@dataclass
class ClientFetchModels:
    type: str = "fetch_models"
    backend_type: str = ""
    api_key: str = ""
    base_url: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientFetchModels":
        return cls(
            type="fetch_models",
            backend_type=d.get("backend_type", ""),
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url", ""),
        )


@dataclass
class ClientUninstallSkill:
    type: str = "uninstall_skill"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUninstallSkill":
        return cls(
            type="uninstall_skill",
            name=d.get("name", ""),
        )


@dataclass
class ClientUpdateSkill:
    type: str = "update_skill"
    name: str = ""
    content: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateSkill":
        return cls(
            type="update_skill",
            name=d.get("name", ""),
            content=d.get("content", ""),
        )


@dataclass
class ClientUpdateSkills:
    type: str = "update_skills"
    enabled_skills: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateSkills":
        return cls(
            type="update_skills",
            enabled_skills=d.get("enabled_skills", []),
        )


@dataclass
class ClientInstallSkill:
    type: str = "install_skill"
    name: str = ""
    content: str = ""
    file_path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientInstallSkill":
        return cls(
            type="install_skill",
            name=d.get("name", ""),
            content=d.get("content", ""),
            file_path=d.get("file_path", ""),
        )


@dataclass
class ClientUpdateMCP:
    type: str = "update_mcp"
    # Accept either list[dict] (old format) or dict[str, dict] (standard map format)
    mcp_servers: Any = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateMCP":
        return cls(
            type="update_mcp",
            mcp_servers=d.get("mcp_servers", []),
        )


@dataclass
class ClientUpdateAgent:
    type: str = "update_agent"
    system_prompt: str = ""
    specialty: str = "general"
    permission_mode: str = ""
    max_turns: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateAgent":
        return cls(
            type="update_agent",
            system_prompt=d.get("system_prompt", ""),
            specialty=d.get("specialty", "general"),
            permission_mode=d.get("permission_mode", ""),
            max_turns=d.get("max_turns", 0),
        )


@dataclass
class ClientSearch:
    type: str = "search"
    query: str = ""
    seq: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSearch":
        return cls(
            type="search",
            query=d.get("query", ""),
            seq=d.get("seq", 0),
        )


@dataclass
class ClientRollbackLog:
    type: str = "rollback_log"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRollbackLog":
        return cls(
            type="rollback_log",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientRollbackCheckout:
    type: str = "rollback_checkout"
    session_id: str = ""
    commit_hash: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRollbackCheckout":
        return cls(
            type="rollback_checkout",
            session_id=d.get("session_id", ""),
            commit_hash=d.get("commit_hash", ""),
        )


@dataclass
class ClientValidateModel:
    type: str = "validate_model"
    backend_type: str = ""
    api_key: str = ""
    base_url: str = ""
    model_id: str = ""
    max_tokens: int = 4096
    name: str = ""
    # Index of the model being edited; -1 (default) means "add new".
    model_index: int = -1
    multimodal: bool = False
    thinking_config: dict[str, Any] | None = field(default_factory=lambda: {"type": "enabled", "enabled": True})

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientValidateModel":
        return cls(
            type="validate_model",
            backend_type=d.get("backend_type", ""),
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url", ""),
            model_id=d.get("model_id", ""),
            max_tokens=d.get("max_tokens", 4096),
            name=d.get("name", ""),
            model_index=d.get("model_index", -1),
            multimodal=d.get("multimodal", False),
            thinking_config=d.get("thinking_config") if d.get("thinking_config") is not None else {"type": "enabled", "enabled": True},
        )


@dataclass
class ClientEditMessage:
    type: str = "edit_message"
    message_index: int = 0
    new_content: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientEditMessage":
        return cls(
            type="edit_message",
            message_index=d.get("message_index", 0),
            new_content=d.get("new_content", ""),
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientDeleteMessage:
    type: str = "delete_message"
    message_index: int = 0
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientDeleteMessage":
        return cls(
            type="delete_message",
            message_index=d.get("message_index", 0),
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientDeleteSession:
    type: str = "delete_session"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientDeleteSession":
        return cls(
            type="delete_session",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientExportSession:
    type: str = "export_session"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientExportSession":
        return cls(
            type="export_session",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientExportSessionsBatch:
    type: str = "export_sessions_batch"
    session_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientExportSessionsBatch":
        return cls(
            type="export_sessions_batch",
            session_ids=d.get("session_ids", []),
        )


@dataclass
class ClientRenameSession:
    type: str = "rename_session"
    session_id: str = ""
    new_name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRenameSession":
        return cls(
            type="rename_session",
            session_id=d.get("session_id", ""),
            new_name=d.get("new_name", ""),
        )


@dataclass
class ClientArchiveSession:
    """Archive a session: hide it from the normal sidebar (data untouched)."""

    type: str = "archive_session"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientArchiveSession":
        return cls(
            type="archive_session",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientUnarchiveSession:
    """Restore a previously archived session back into the normal lists."""

    type: str = "unarchive_session"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUnarchiveSession":
        return cls(
            type="unarchive_session",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientListArchivedSessions:
    """Request the list of archived sessions for the archive manager view."""

    type: str = "list_archived_sessions"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListArchivedSessions":
        return cls(type="list_archived_sessions")


@dataclass
class ClientIclawResume:
    type: str = "iclaw_resume"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientIclawResume":
        return cls(type="iclaw_resume")


@dataclass
class ClientAgentCreate:
    type: str = "agent_create"
    agent: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAgentCreate":
        return cls(type="agent_create", agent=d.get("agent", {}))


@dataclass
class ClientAgentDelete:
    type: str = "agent_delete"
    index: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAgentDelete":
        return cls(type="agent_delete", index=d.get("index", 0))


@dataclass
class ClientAgentUpdate:
    type: str = "agent_update"
    index: int = 0
    agent: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAgentUpdate":
        return cls(type="agent_update", index=d.get("index", 0), agent=d.get("agent", {}))


@dataclass
class ClientAgentList:
    type: str = "agent_list"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientAgentList":
        return cls(type="agent_list")


@dataclass
class ClientAgentSetActive:
    type: str = "agent_set_active"
    index: int = -1

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAgentSetActive":
        return cls(type="agent_set_active", index=d.get("index", -1))


@dataclass
class ClientUpdateSubAgents:
    type: str = "update_sub_agents"
    sub_agents: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUpdateSubAgents":
        return cls(
            type="update_sub_agents",
            sub_agents=d.get("agents", d.get("sub_agents", [])),
        )


@dataclass
class ClientOpenWorkspace:
    type: str = "open_workspace"
    path: str = ""
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientOpenWorkspace":
        return cls(
            type="open_workspace",
            path=d.get("path", ""),
            request_id=d.get("request_id", ""),
        )


@dataclass
class ClientListWorkspaces:
    type: str = "list_workspaces"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListWorkspaces":
        return cls(type="list_workspaces")


@dataclass
class ClientRemoveWorkspace:
    type: str = "remove_workspace"
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRemoveWorkspace":
        return cls(
            type="remove_workspace",
            path=d.get("path", ""),
        )


@dataclass
class ClientCloseWorkspace:
    type: str = "close_workspace"
    request_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientCloseWorkspace":
        return cls(type="close_workspace", request_id=d.get("request_id", ""))


@dataclass
class ClientUploadWorkspaceIcon:
    type: str = "upload_workspace_icon"
    path: str = ""
    # Full data URL of the picked image: "data:image/png;base64,..."
    icon_data: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientUploadWorkspaceIcon":
        return cls(
            type="upload_workspace_icon",
            path=d.get("path", ""),
            icon_data=d.get("icon_data", ""),
        )


@dataclass
class ClientRenameWorkspace:
    type: str = "rename_workspace"
    path: str = ""
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRenameWorkspace":
        return cls(
            type="rename_workspace",
            path=d.get("path", ""),
            name=d.get("name", ""),
        )


@dataclass
class ClientGetWorkspaceConfig:
    type: str = "get_workspace_config"
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientGetWorkspaceConfig":
        return cls(
            type="get_workspace_config",
            path=d.get("path", ""),
        )


@dataclass
class ClientSaveWorkspaceConfig:
    type: str = "save_workspace_config"
    path: str = ""
    # Partial per-workspace config: {"models": [model_id,...] | null,
    #                              "permissions": {...} | null,
    #                              "mcp": {...} | null}
    # null/absent fields mean "reuse the global config for this key".
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSaveWorkspaceConfig":
        return cls(
            type="save_workspace_config",
            path=d.get("path", ""),
            config=d.get("config") or {},
        )


@dataclass
class ClientReindexWorkspace:
    type: str = "reindex_workspace"
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientReindexWorkspace":
        return cls(type="reindex_workspace", path=d.get("path", ""))


@dataclass
class ClientGetGitignore:
    type: str = "get_gitignore"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientGetGitignore":
        return cls(type="get_gitignore")


@dataclass
class ClientDeleteIndex:
    type: str = "delete_index"
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientDeleteIndex":
        return cls(type="delete_index", path=d.get("path", ""))


@dataclass
class ClientGetIndexStatus:
    type: str = "get_index_status"
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientGetIndexStatus":
        return cls(type="get_index_status", path=d.get("path", ""))


@dataclass
class ClientSetGitignore:
    type: str = "set_gitignore"
    content: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSetGitignore":
        return cls(
            type="set_gitignore",
            content=d.get("content", ""),
        )


@dataclass
class ClientAddDocument:
    type: str = "add_document"
    name: str = ""
    file_path: str = ""
    url: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAddDocument":
        return cls(
            type="add_document",
            name=d.get("name", ""),
            file_path=d.get("file_path", ""),
            url=d.get("url", ""),
        )


@dataclass
class ClientRemoveDocument:
    type: str = "remove_document"
    id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRemoveDocument":
        return cls(
            type="remove_document",
            id=d.get("id", ""),
        )


@dataclass
class ClientListDocuments:
    type: str = "list_documents"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListDocuments":
        return cls(type="list_documents")


@dataclass
class ClientGetMemoryList:
    type: str = "get_memory_list"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientGetMemoryList":
        return cls(type="get_memory_list")


@dataclass
class ClientGetProfile:
    type: str = "get_profile"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientGetProfile":
        return cls(type="get_profile")


@dataclass
class ClientGetMemoryDetail:
    type: str = "get_memory_detail"
    path: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientGetMemoryDetail":
        return cls(type="get_memory_detail", path=d.get("path", ""))


@dataclass
class ClientListGlobalRules:
    type: str = "list_global_rules"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListGlobalRules":
        return cls(type="list_global_rules")


@dataclass
class ClientListProjectRules:
    type: str = "list_project_rules"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListProjectRules":
        return cls(type="list_project_rules")


@dataclass
class ClientListProjectHooks:
    type: str = "list_project_hooks"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientListProjectHooks":
        return cls(type="list_project_hooks")


@dataclass
class ClientSaveGlobalRule:
    type: str = "save_global_rule"
    name: str = ""
    content: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSaveGlobalRule":
        return cls(
            type="save_global_rule",
            name=d.get("name", ""),
            content=d.get("content", ""),
        )


@dataclass
class ClientDeleteGlobalRule:
    type: str = "delete_global_rule"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientDeleteGlobalRule":
        return cls(
            type="delete_global_rule",
            name=d.get("name", ""),
        )


@dataclass
class ClientGetGlobalRuleContent:
    type: str = "get_global_rule_content"
    name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientGetGlobalRuleContent":
        return cls(
            type="get_global_rule_content",
            name=d.get("name", ""),
        )


@dataclass
class ClientTerminalSpawn:
    type: str = "terminal_spawn"
    shell: str = ""
    shell_args: list[str] | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientTerminalSpawn":
        return cls(
            type="terminal_spawn",
            shell=d.get("shell", ""),
            shell_args=d.get("shell_args"),
        )


@dataclass
class ClientTerminalWrite:
    type: str = "terminal_write"
    id: int = 0
    data: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientTerminalWrite":
        return cls(type="terminal_write", id=d.get("id", 0), data=d.get("data", ""))


@dataclass
class ClientTerminalResize:
    type: str = "terminal_resize"
    id: int = 0
    cols: int = 80
    rows: int = 24

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientTerminalResize":
        return cls(type="terminal_resize", id=d.get("id", 0), cols=d.get("cols", 80), rows=d.get("rows", 24))


@dataclass
class ClientTerminalKill:
    type: str = "terminal_kill"
    id: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientTerminalKill":
        return cls(type="terminal_kill", id=d.get("id", 0))


@dataclass
class ClientTerminalListShells:
    type: str = "terminal_list_shells"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientTerminalListShells":
        return cls(type="terminal_list_shells")


@dataclass
class ClientRetry:
    type: str = "retry"
    user_message_index: int = 0
    session_id: str = ""
    mode: str = "normal"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRetry":
        return cls(
            type="retry",
            user_message_index=d.get("user_message_index", 0),
            session_id=d.get("session_id", ""),
            mode=d.get("mode", "normal"),
        )


@dataclass
class ClientSwitchBranch:
    type: str = "switch_branch"
    branch_id: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientSwitchBranch":
        return cls(
            type="switch_branch",
            branch_id=d.get("branch_id", ""),
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientRollbackBranch:
    type: str = "rollback"
    branch_id: str = ""
    message_id: str = ""
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientRollbackBranch":
        return cls(
            type="rollback",
            branch_id=d.get("branch_id", ""),
            message_id=d.get("message_id", ""),
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientGetUsageStats:
    type: str = "get_usage_stats"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientGetUsageStats":
        return cls(type="get_usage_stats")


@dataclass
class ClientReplayGetSession:
    """Request a session's recorded telemetry for replay / scrubbing.

    Returns the full ordered event stream plus a summary and turn-boundary
    indices so the frontend can render a cursor-based scrubber without
    extra round-trips.
    """
    type: str = "replay_get_session"
    session_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientReplayGetSession":
        return cls(
            type="replay_get_session",
            session_id=d.get("session_id", ""),
        )


@dataclass
class ClientAutomationListJobs:
    type: str = "automation_list_jobs"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientAutomationListJobs":
        return cls(type="automation_list_jobs")


@dataclass
class ClientAutomationCreateJob:
    type: str = "automation_create_job"
    name: str = ""
    prompt: str = ""
    cron: str = ""
    tag: str = ""
    model_index: int = -1
    push_gateways: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationCreateJob":
        return cls(
            type="automation_create_job",
            name=d.get("name", ""),
            prompt=d.get("prompt", ""),
            cron=d.get("cron", ""),
            tag=d.get("tag", ""),
            model_index=d.get("model_index", -1),
            push_gateways=d.get("push_gateways", []),
        )


@dataclass
class ClientAutomationCancelJob:
    type: str = "automation_cancel_job"
    job_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationCancelJob":
        return cls(
            type="automation_cancel_job",
            job_id=d.get("job_id", ""),
        )


@dataclass
class ClientAutomationGetHistory:
    type: str = "automation_get_history"

    @classmethod
    def from_dict(cls, _d: dict[str, Any]) -> "ClientAutomationGetHistory":
        return cls(type="automation_get_history")


@dataclass
class ClientAutomationToggleJob:
    type: str = "automation_toggle_job"
    job_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationToggleJob":
        return cls(
            type="automation_toggle_job",
            job_id=d.get("job_id", ""),
        )


@dataclass
class ClientAutomationUpdateJob:
    type: str = "automation_update_job"
    job_id: str = ""
    name: str = ""
    prompt: str = ""
    cron: str = ""
    tag: str = ""
    model_index: int = -1
    push_gateways: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationUpdateJob":
        return cls(
            type="automation_update_job",
            job_id=d.get("job_id", ""),
            name=d.get("name", ""),
            prompt=d.get("prompt", ""),
            cron=d.get("cron", ""),
            tag=d.get("tag", ""),
            model_index=d.get("model_index", -1),
            push_gateways=d.get("push_gateways", []),
        )


@dataclass
class ClientAutomationDeleteJob:
    type: str = "automation_delete_job"
    job_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationDeleteJob":
        return cls(
            type="automation_delete_job",
            job_id=d.get("job_id", ""),
        )


@dataclass
class ClientAutomationDeleteExecution:
    type: str = "automation_delete_execution"
    entry_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationDeleteExecution":
        return cls(
            type="automation_delete_execution",
            entry_id=d.get("entry_id", ""),
        )


@dataclass
class ClientAutomationRenameExecution:
    type: str = "automation_rename_execution"
    entry_id: str = ""
    new_name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClientAutomationRenameExecution":
        return cls(
            type="automation_rename_execution",
            entry_id=d.get("entry_id", ""),
            new_name=d.get("new_name", ""),
        )


# The discriminated union of every client message type understood by the handler.
# ``parse_client_message`` returns one of these based on the wire ``type`` field.
ClientMessage = (
    ClientRun
    | ClientRespondPermission
    | ClientCancel
    | ClientResume
    | ClientConfigure
    | ClientPing
    | ClientListModels
    | ClientListSessions
    | ClientListAllSessions
    | ClientNewSession
    | ClientGetConfig
    | ClientUpdateModels
    | ClientSetActiveModel
    | ClientDeleteModel
    | ClientFetchModels
    | ClientUpdateSkills
    | ClientInstallSkill
    | ClientUninstallSkill
    | ClientUpdateSkill
    | ClientUpdateMCP
    | ClientUpdateAgent
    | ClientSearch
    | ClientRollbackLog
    | ClientRollbackCheckout
    | ClientValidateModel
    | ClientEditMessage
    | ClientDeleteMessage
    | ClientDeleteSession
    | ClientExportSession
    | ClientExportSessionsBatch
    | ClientRenameSession
    | ClientAgentCreate
    | ClientAgentDelete
    | ClientAgentUpdate
    | ClientAgentList
    | ClientAgentSetActive
    | ClientUpdateSubAgents
    | ClientOpenWorkspace
    | ClientListWorkspaces
    | ClientRemoveWorkspace
    | ClientCloseWorkspace
    | ClientUploadWorkspaceIcon
    | ClientRenameWorkspace
    | ClientGetWorkspaceConfig
    | ClientSaveWorkspaceConfig
    | ClientReindexWorkspace
    | ClientGetGitignore
    | ClientDeleteIndex
    | ClientGetIndexStatus
    | ClientSetGitignore
    | ClientGetMemoryList
    | ClientGetMemoryDetail
    | ClientListGlobalRules
    | ClientListProjectRules
    | ClientListProjectHooks
    | ClientSaveGlobalRule
    | ClientDeleteGlobalRule
    | ClientGetGlobalRuleContent
    | ClientGetProfile
    | ClientTerminalSpawn
    | ClientTerminalWrite
    | ClientTerminalResize
    | ClientTerminalKill
    | ClientTerminalListShells
    | ClientRetry
    | ClientSwitchBranch
    | ClientRollbackBranch
    | ClientGetUsageStats
    | ClientWechatScan
    | ClientIclawResume
    | ClientAutomationListJobs
    | ClientAutomationCreateJob
    | ClientAutomationCancelJob
    | ClientAutomationGetHistory
    | ClientAutomationToggleJob
    | ClientAutomationUpdateJob
    | ClientAutomationDeleteJob
    | ClientAutomationDeleteExecution
    | ClientAutomationRenameExecution
    | ClientSpecApprove
    | ClientSpecReject
    | ClientPlanApprove
    | ClientPlanReject
    | ClientSetMode
    | ClientSetCdpUrl
    | ClientExportData
    | ClientImportData
)


def parse_client_message(raw: str | bytes) -> ClientMessage | None:
    """Parse a raw WebSocket frame into the matching ``Client*`` message.

    Decrypts the payload if it is ciphertext (see
    :func:`_parse_client_encrypted`), then dispatches on the ``type``
    field via the ``parsers`` table.  Returns ``None`` for unknown or
    unparseable frames so the handler can emit a safe error.
    """
    decrypted = _parse_client_encrypted(raw)
    try:
        if isinstance(decrypted, bytes):
            decrypted = decrypted.decode("utf-8")
        data = json.loads(decrypted)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    msg_type = data.get("type", "")
    parsers: dict[str, type] = {
        "run": ClientRun,
        "respond_permission": ClientRespondPermission,
        "respond_plan": ClientRespondPlan,
        "set_plan_mode": ClientSetPlanMode,
        "set_command": ClientSetCommand,
        "respond_question": ClientRespondQuestion,
        "cancel": ClientCancel,
        "resume": ClientResume,
        "configure": ClientConfigure,
        "ping": ClientPing,
        "list_models": ClientListModels,
        "list_sessions": ClientListSessions,
        "list_all_sessions": ClientListAllSessions,
        "new_session": ClientNewSession,
        "get_config": ClientGetConfig,
        "wechat_scan": ClientWechatScan,
        "iclaw_resume": ClientIclawResume,
        "update_models": ClientUpdateModels,
        "set_active_model": ClientSetActiveModel,
        "delete_model": ClientDeleteModel,
        "fetch_models": ClientFetchModels,
        "update_skills": ClientUpdateSkills,
        "install_skill": ClientInstallSkill,
        "uninstall_skill": ClientUninstallSkill,
        "update_skill": ClientUpdateSkill,
        "update_mcp": ClientUpdateMCP,
        "update_agent": ClientUpdateAgent,
        "search": ClientSearch,
        "rollback_log": ClientRollbackLog,
        "rollback_checkout": ClientRollbackCheckout,
        "validate_model": ClientValidateModel,
        "edit_message": ClientEditMessage,
        "delete_message": ClientDeleteMessage,
        "delete_session": ClientDeleteSession,
        "export_session": ClientExportSession,
        "export_sessions_batch": ClientExportSessionsBatch,
        "rename_session": ClientRenameSession,
        "archive_session": ClientArchiveSession,
        "unarchive_session": ClientUnarchiveSession,
        "list_archived_sessions": ClientListArchivedSessions,
        "agent_create": ClientAgentCreate,
        "agent_delete": ClientAgentDelete,
        "agent_update": ClientAgentUpdate,
        "agent_list": ClientAgentList,
        "agent_set_active": ClientAgentSetActive,
        "update_sub_agents": ClientUpdateSubAgents,
        "open_workspace": ClientOpenWorkspace,
        "list_workspaces": ClientListWorkspaces,
        "remove_workspace": ClientRemoveWorkspace,
        "close_workspace": ClientCloseWorkspace,
        "upload_workspace_icon": ClientUploadWorkspaceIcon,
        "rename_workspace": ClientRenameWorkspace,
        "get_workspace_config": ClientGetWorkspaceConfig,
        "save_workspace_config": ClientSaveWorkspaceConfig,
        "reindex_workspace": ClientReindexWorkspace,
        "get_gitignore": ClientGetGitignore,
        "delete_index": ClientDeleteIndex,
        "get_index_status": ClientGetIndexStatus,
        "set_gitignore": ClientSetGitignore,
        "add_document": ClientAddDocument,
        "remove_document": ClientRemoveDocument,
        "list_documents": ClientListDocuments,
        "get_memory_list": ClientGetMemoryList,
        "get_memory_detail": ClientGetMemoryDetail,
        "list_global_rules": ClientListGlobalRules,
        "list_project_rules": ClientListProjectRules,
        "list_project_hooks": ClientListProjectHooks,
        "save_global_rule": ClientSaveGlobalRule,
        "delete_global_rule": ClientDeleteGlobalRule,
        "get_global_rule_content": ClientGetGlobalRuleContent,
        "get_profile": ClientGetProfile,
        "terminal_spawn": ClientTerminalSpawn,
        "terminal_write": ClientTerminalWrite,
        "terminal_resize": ClientTerminalResize,
        "terminal_kill": ClientTerminalKill,
        "terminal_list_shells": ClientTerminalListShells,
        "retry": ClientRetry,
        "switch_branch": ClientSwitchBranch,
        "rollback": ClientRollbackBranch,
        "get_usage_stats": ClientGetUsageStats,
        "replay_get_session": ClientReplayGetSession,
        "automation_list_jobs": ClientAutomationListJobs,
        "automation_create_job": ClientAutomationCreateJob,
        "automation_cancel_job": ClientAutomationCancelJob,
        "automation_get_history": ClientAutomationGetHistory,
        "automation_toggle_job": ClientAutomationToggleJob,
        "automation_update_job": ClientAutomationUpdateJob,
        "automation_delete_job": ClientAutomationDeleteJob,
        "automation_delete_execution": ClientAutomationDeleteExecution,
        "automation_rename_execution": ClientAutomationRenameExecution,
        "engine_install_response": ClientEngineInstallResponse,
        "spec_approve": ClientSpecApprove,
        "spec_reject": ClientSpecReject,
        "plan_approve": ClientPlanApprove,
        "plan_reject": ClientPlanReject,
        "set_mode": ClientSetMode,
        "browser_cdp_url": ClientSetCdpUrl,
        "export_data": ClientExportData,
        "import_data": ClientImportData,
    }
    cls = parsers.get(msg_type)
    if cls is None:
        return None
    return cls.from_dict(data)


# 鈹€鈹€ Server -> Client message types 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

ServerMessageType = Literal[
    "text_delta",
    "thinking_delta",
    "tool_call_start",
    "tool_call_delta",
    "tool_call_end",
    "tool_progress",
    "tool_result",
    "permission_request",
    "finish",
    "pong",
    "error",
    "session_ready",
    "configured",
    "telemetry",
    "plan_update",
    "agent_state",
    "spec_update",
    "plan_review",
    "models_list",
    "sessions_list",
    "config_data",
    "models_updated",
    "models_fetched",
    "skills_updated",
    "skills_list",
    "skill_installed",
    "skill_install_error",
    "skill_uninstalled",
    "mcp_updated",
    "agent_updated",
    "search_results",
    "compact",
    "compact_starting",
    "compact_failed",
    "rollback_log",
    "rollback_checkout",
    "model_validated",
    "model_validation_error",
    "messages_updated",
    "session_deleted",
    "session_exported",
    "sessions_exported_zip",
    "session_renamed",
    "archived_sessions_list",
    "memory_list",
    "memory_detail",
    "global_rules_list",
    "project_rules_list",
    "global_rule_saved",
    "global_rule_deleted",
    "global_rule_content",
    "profile_data",
    "agents_list",
    "agents_updated",
    "sub_agents_updated",
    "index_status",
    "gitignore_content",
    "documents_list",
    "document_added",
    "document_removed",
    "document_error",
    "artifacts_update",
    "assistant_boundary",
    "slash_commands",
    "terminal_data",
    "terminal_shells",
    "terminal_spawned",
    "branch_switched",
    "branch_rolled_back",
    "branch_updated",
    "usage_stats",
    "automation_jobs_list",
    "automation_job_created",
    "automation_job_cancelled",
    "automation_job_history",
    "automation_job_update",
    "automation_stream_event",
    "mode_changed",
    "plan_mode_changed",
    "command_changed",
    "data_exported_zip",
    "data_export_progress",
    "data_import_progress",
    "data_import_done",
]


def _make_message(msg_type: ServerMessageType, **kwargs: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"type": msg_type}
    msg.update(kwargs)
    return msg


def encode_server_message(
    msg_type: ServerMessageType,
    encrypt: bool = True,
    **kwargs: Any,
) -> str:
    if encrypt:
        return encode_server_encrypted(msg_type, **kwargs)
    return json.dumps({"type": msg_type, **kwargs}, ensure_ascii=False)


# 鈹€鈹€ Convenience encoders 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€


def encode_text_delta(text: str) -> str:
    return encode_server_message("text_delta", encrypt=False, text=text)


def encode_thinking_delta(text: str) -> str:
    return encode_server_message("thinking_delta", encrypt=False, text=text)


def encode_tool_call_start(name: str, call_id: str) -> str:
    return encode_server_message("tool_call_start", encrypt=False, name=name, id=call_id)


def encode_tool_call_delta(call_id: str, key: str, value: str) -> str:
    return encode_server_message("tool_call_delta", encrypt=False, id=call_id, key=key, value=value)


def encode_tool_call_end(call_id: str) -> str:
    return encode_server_message("tool_call_end", encrypt=False, id=call_id)


def encode_tool_progress(call_id: str, tool_name: str, status: str) -> str:
    return encode_server_message("tool_progress", encrypt=False, id=call_id, tool_name=tool_name, status=status)


def encode_tool_result(
    call_id: str,
    content: str,
    is_error: bool = False,
    sub_agent_messages: list[dict[str, Any]] | None = None,
    sub_agent_session_id: str | None = None,
) -> str:
    return encode_server_message(
        "tool_result", encrypt=False,
        id=call_id,
        content=content,
        is_error=is_error,
        sub_agent_messages=sub_agent_messages,
        sub_agent_session_id=sub_agent_session_id,
    )


def encode_permission_request(tool_name: str, reason: str) -> str:
    return encode_server_message("permission_request", encrypt=False, tool_name=tool_name, reason=reason)


def encode_finish(reason: str, usage: dict[str, Any] | None = None, error: str | None = None) -> str:
    return encode_server_message("finish", encrypt=False, reason=reason, usage=usage, error=error)


def encode_pong() -> str:
    return encode_server_message("pong", encrypt=False)


def encode_documents_list(documents: list[dict[str, Any]]) -> str:
    return encode_server_message("documents_list", documents=documents)


def encode_document_added(document: dict[str, Any]) -> str:
    return encode_server_message("document_added", document=document)


def encode_document_updated(document: dict[str, Any]) -> str:
    return encode_server_message("document_updated", document=document)


def encode_document_removed(id: str) -> str:
    return encode_server_message("document_removed", id=id)


def encode_document_error(message: str) -> str:
    return encode_server_message("document_error", message=message)


def encode_error(message: str, code: str = "internal") -> str:
    return encode_server_message("error", encrypt=False, message=message, code=code)


def encode_session_ready(
    session_id: str,
    messages: list[dict[str, Any]] | None = None,
    request_id: str = "",
) -> str:
    payload: dict[str, Any] = {"session_id": session_id}
    if messages is not None:
        payload["messages"] = messages
    if request_id:
        payload["request_id"] = request_id
    return encode_server_message("session_ready", encrypt=False, **payload)


def encode_configured(config: dict[str, Any]) -> str:
    return encode_server_message("configured", config=config)


def encode_telemetry(data: dict[str, Any]) -> str:
    return encode_server_message("telemetry", data=data)


def encode_plan_update(plan_items: list[dict[str, Any]]) -> str:
    return encode_server_message("plan_update", plan_items=plan_items)


def encode_agent_state(state: dict[str, Any]) -> str:
    return encode_server_message("agent_state", state=state)


def encode_spec_update(
    spec: dict[str, Any] | None,
    status: str = "",
    feedback: str = "",
) -> str:
    """Encode a spec update event for the frontend."""
    return encode_server_message("spec_update", spec=spec, status=status, feedback=feedback)


def encode_plan_review(
    review: dict[str, Any] | None,
    status: str = "review",
    feedback: str = "",
) -> str:
    """Encode a plan review event for the frontend."""
    return encode_server_message("plan_review", review=review, status=status, feedback=feedback)


def encode_models_list(models: list[str]) -> str:
    return encode_server_message("models_list", models=models)


def encode_sessions_list(sessions: list[dict[str, Any]]) -> str:
    return encode_server_message("sessions_list", sessions=sessions)


def encode_archived_sessions_list(sessions: list[dict[str, Any]]) -> str:
    return encode_server_message("archived_sessions_list", sessions=sessions)


def encode_config_data(config: dict[str, Any]) -> str:
    return encode_server_message("config_data", config=config)


def encode_models_updated(models: list[dict[str, Any]], active_model_index: int) -> str:
    return encode_server_message("models_updated", models=models, active_model_index=active_model_index)


def encode_models_fetched(models: list[str]) -> str:
    return encode_server_message("models_fetched", models=models)


def encode_skills_updated(enabled_skills: list[str], available_skills: list[dict[str, Any]]) -> str:
    return encode_server_message("skills_updated", enabled_skills=enabled_skills, available_skills=available_skills)


def encode_skills_list(skills: list[dict[str, Any]]) -> str:
    return encode_server_message("skills_list", skills=skills)


def encode_skill_installed(name: str, available_skills: list[dict[str, Any]]) -> str:
    return encode_server_message("skill_installed", name=name, available_skills=available_skills)


def encode_skill_install_error(name: str, message: str) -> str:
    return encode_server_message("skill_install_error", name=name, message=message)


def encode_skill_uninstalled(name: str, available_skills: list[dict[str, Any]]) -> str:
    return encode_server_message("skill_uninstalled", name=name, available_skills=available_skills)


def encode_mcp_updated(mcp_servers: list[dict[str, Any]]) -> str:
    return encode_server_message("mcp_updated", mcp_servers=mcp_servers)


def encode_agent_updated(config: dict[str, Any]) -> str:
    return encode_server_message("agent_updated", config=config)


def encode_search_results(results: list[dict[str, Any]]) -> str:
    return encode_server_message("search_results", results=results)


def encode_rollback_log(session_id: str, commits: list[dict[str, Any]]) -> str:
    return encode_server_message("rollback_log", session_id=session_id, commits=commits)


def encode_rollback_checkout(session_id: str, commit_hash: str, messages: list[dict[str, Any]], turn_count: int, plan_items: list[dict[str, Any]] | None = None, artifacts: list[dict[str, Any]] | None = None, references: list[dict[str, Any]] | None = None) -> str:
    return encode_server_message("rollback_checkout", session_id=session_id, commit_hash=commit_hash, messages=messages, turn_count=turn_count, plan_items=plan_items or [], artifacts=artifacts or [], references=references or [])


def encode_messages_updated(messages: list[dict[str, Any]], session_id: str, commit_hash: str = "") -> str:
    return encode_server_message("messages_updated", messages=messages, session_id=session_id, commit_hash=commit_hash)


def encode_session_deleted(session_id: str) -> str:
    return encode_server_message("session_deleted", session_id=session_id)


def encode_session_exported(session_id: str, markdown: str, filename: str) -> str:
    return encode_server_message("session_exported", session_id=session_id, markdown=markdown, filename=filename)


def encode_sessions_exported_zip(zip_base64: str, filename: str) -> str:
    return encode_server_message("sessions_exported_zip", encrypt=False, zip_base64=zip_base64, filename=filename)


def encode_data_exported_zip(zip_path: str, filename: str, request_id: str = "") -> str:
    return encode_server_message("data_exported_zip", encrypt=False, zip_path=zip_path, filename=filename, request_id=request_id)


def encode_data_export_progress(done: int, total: int, percent: int, file: str = "") -> str:
    return encode_server_message(
        "data_export_progress", encrypt=False, done=done, total=total, percent=percent, file=file
    )


def encode_data_import_progress(done: int, total: int, percent: int, file: str = "") -> str:
    return encode_server_message(
        "data_import_progress", encrypt=False, done=done, total=total, percent=percent, file=file
    )


def encode_data_import_done(files: int, restored: int, skipped: int, overwritten: int = 0, kept: int = 0) -> str:
    return encode_server_message(
        "data_import_done",
        encrypt=False,
        files=files,
        restored=restored,
        skipped=skipped,
        overwritten=overwritten,
        kept=kept,
    )


def encode_session_renamed(session_id: str, new_name: str) -> str:
    return encode_server_message("session_renamed", session_id=session_id, new_name=new_name)


def encode_agents_list(agents: list[dict[str, Any]], active_index: int) -> str:
    return encode_server_message("agents_list", agents=agents, active_index=active_index)


def encode_agents_updated(agents: list[dict[str, Any]], active_index: int) -> str:
    return encode_server_message("agents_updated", agents=agents, active_index=active_index)


def encode_sub_agents_updated(sub_agents: list[dict[str, Any]]) -> str:
    return encode_server_message("sub_agents_updated", sub_agents=sub_agents)


def encode_artifacts_update(artifacts: list[dict[str, Any]]) -> str:
    return encode_server_message("artifacts_update", artifacts=artifacts)


def encode_assistant_boundary() -> str:
    return encode_server_message("assistant_boundary")
